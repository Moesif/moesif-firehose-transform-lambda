import base64
import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# The lambda function lives at the project root, one level up from this file.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lambda_function as lf

def event(**overrides):
    base = {
        "request": {"verb": "GET", "uri": "https://api.example.com/v1/items",
                    "headers": {"X-Client-Id": "acme-corp"}},
        "response": {"status": 200},
        "company_id": "acme-corp",
    }
    base.update(overrides)
    return base


def config(rules=(), default=100, user_sample_rate=None, company_sample_rate=None):
    return {"default_sample_rate": float(default), "rules": list(rules),
            "user_sample_rate": user_sample_rate or {},
            "company_sample_rate": company_sample_rate or {},
            "source": "test", "valid": True}


def rule(name, rate, conditions):
    return lf._parse_rule({"name": name, "sample_rate": rate, "conditions": conditions}, 0)


def firehose(*bodies):
    return {"invocationId": "test", "records": [
        {"recordId": "r%d" % i, "data": base64.b64encode(b.encode()).decode()}
        for i, b in enumerate(bodies)]}


def body_of(record):
    return base64.b64decode(record["data"]).decode()


class TestRateResolution(unittest.TestCase):
    def test_first_matching_rule_wins(self):
        cfg = config([
            rule("errors", 100, [{"path": "response.status", "operator": "gte", "value": 400}]),
            rule("acme", 10, [{"path": "company_id", "value": "acme-corp"}]),
        ])
        # A 500 from acme matches both rules; declaration order decides.
        self.assertEqual(lf.resolve_rate(event(response={"status": 500}), cfg)[1], "errors")
        self.assertEqual(lf.resolve_rate(event(), cfg)[1], "acme")

    def test_falls_through_to_the_default(self):
        cfg = config([rule("globex", 10, [{"path": "company_id", "value": "globex"}])], default=50)
        self.assertEqual(lf.resolve_rate(event(), cfg), (50.0, None))

    def test_conditions_are_anded(self):
        cfg = config([rule("both", 10, [
            {"path": "company_id", "value": "acme-corp"},
            {"path": "response.status", "operator": "between", "value": [200, 299]},
        ])])
        self.assertEqual(lf.resolve_rate(event(), cfg)[1], "both")
        self.assertIsNone(lf.resolve_rate(event(response={"status": 500}), cfg)[1])


class TestOperators(unittest.TestCase):
    def match(self, **condition):
        return lf._matches(lf._parse_condition(condition), event())

    def test_status_matches_across_string_and_number(self):
        self.assertTrue(self.match(path="response.status", value=200))
        self.assertTrue(self.match(path="response.status", value="200"))

    def test_between_is_inclusive(self):
        self.assertTrue(self.match(path="response.status", operator="between", value=[200, 299]))
        self.assertFalse(self.match(path="response.status", operator="between", value=[201, 299]))

    def test_header_paths_ignore_casing(self):
        self.assertTrue(self.match(path="request.headers.x-client-id", value="acme-corp"))

    def test_ignore_case_on_values(self):
        self.assertFalse(self.match(path="company_id", value="ACME-CORP"))
        self.assertTrue(self.match(path="company_id", value="ACME-CORP", ignore_case=True))

    def test_absent_field_never_matches(self):
        self.assertFalse(self.match(path="user_id", value="anything"))
        self.assertFalse(self.match(path="user_id", operator="exists"))

    def test_request_route_strips_scheme_host_and_query(self):
        # request.route is the path alone, so an anchored rule matches it.
        self.assertTrue(self.match(path="request.route", operator="regex", value="^/v1/items$"))
        self.assertFalse(self.match(path="request.uri", operator="regex", value="^/v1/items$"))

    def test_request_route_falls_back_to_slash(self):
        payload = {"request": {"uri": "https://api.example.com"}}
        self.assertEqual(lf._route(payload), "/")

    def test_request_route_prefers_a_real_field_when_present(self):
        payload = {"request": {"uri": "https://api.example.com/v1/items", "route": "/custom"}}
        self.assertEqual(lf.get_path(payload, "request.route"), "/custom")

    def test_regex_and_in(self):
        self.assertTrue(self.match(path="request.uri", operator="regex", value="/v1/items"))
        self.assertTrue(self.match(path="request.verb", operator="in", value=["GET", "HEAD"]))


class TestKeepDropWeight(unittest.TestCase):
    def test_boundaries_need_no_roll(self):
        self.assertTrue(lf.should_keep(100))
        self.assertFalse(lf.should_keep(0))

    def test_rate_is_honoured_over_many_rolls(self):
        random.seed(1234)
        kept = sum(lf.should_keep(10) for _ in range(20000))
        self.assertTrue(1800 < kept < 2200, kept)

    def test_weight_is_the_inverse_of_the_rate(self):
        for rate, expected in [(100, 1), (50, 2), (25, 4), (10, 10), (5, 20), (1, 100)]:
            self.assertEqual(lf.weight_for(rate), expected)

    def test_awkward_rates_floor(self):
        self.assertEqual(lf.weight_for(30), 3)   # Floored; under-reports volume

    def test_rate_zero_does_not_divide_by_zero(self):
        self.assertEqual(lf.weight_for(0), 1)

    def test_full_rate_leaves_the_event_untouched(self):
        payload = event()
        self.assertFalse(lf.stamp_weight(payload, 100))
        self.assertNotIn("weight", payload)      # An absent weight means 1

    def test_existing_weight_is_overwritten(self):
        # Weight is assigned unconditionally, not combined with what was there.
        payload = event(weight=10)
        lf.stamp_weight(payload, 50)
        self.assertEqual(payload["weight"], 2)

    def test_a_stale_weight_is_corrected_even_at_full_rate(self):
        payload = event(weight=10)
        lf.stamp_weight(payload, 100)
        self.assertEqual(payload["weight"], 1)

    def test_an_unusable_existing_weight_is_replaced(self):
        for junk in ("lots", 0, -5, True, False, None, {"a": 1}):
            payload = event(weight=junk)
            lf.stamp_weight(payload, 100)
            self.assertEqual(payload["weight"], 1, repr(junk))

    def test_an_unusable_existing_weight_is_replaced_at_a_sampled_rate(self):
        for junk in ("lots", True, None):
            payload = event(weight=junk)
            lf.stamp_weight(payload, 50)
            self.assertEqual(payload["weight"], 2, repr(junk))

    def test_a_correct_weight_is_left_alone(self):
        payload = event(weight=2)
        self.assertFalse(lf.stamp_weight(payload, 50))
        self.assertEqual(payload["weight"], 2)

    def test_weighted_survivors_reconstruct_volume(self):
        random.seed(7)
        cfg = config([rule("tenth", 10, [{"path": "company_id", "value": "acme-corp"}])])
        total = 0
        for _ in range(10000):
            payload = event()
            if lf.sample(payload, cfg)[0]:
                total += payload["weight"]
        self.assertTrue(9000 < total < 11000, total)


class TestFirehoseContract(unittest.TestCase):
    def setUp(self):
        lf._CONFIG = config([rule("drop acme reads", 0, [
            {"path": "company_id", "value": "acme-corp"},
            {"path": "request.verb", "value": "GET"},
        ])])

    def tearDown(self):
        lf._CONFIG = None

    def test_every_record_id_returns_once_and_never_fails(self):
        out = lf.lambda_handler(firehose(json.dumps(event()), "garbage", ""))["records"]
        self.assertEqual([r["recordId"] for r in out], ["r0", "r1", "r2"])
        # ProcessingFailed would route records to the delivery stream's error output.
        self.assertTrue({r["result"] for r in out} <= {"Ok", "Dropped"})

    def test_matching_events_drop_and_others_survive(self):
        out = lf.lambda_handler(firehose(
            json.dumps(event()),                              # acme GET -> dropped
            json.dumps(event(company_id="globex")),           # -> kept
            json.dumps(event(request={"verb": "POST"})),      # -> kept
        ))["records"]
        self.assertEqual([r["result"] for r in out], ["Dropped", "Ok", "Ok"])

    def test_weight_is_stamped_in_the_output(self):
        lf._CONFIG = config([rule("half", 50, [{"path": "company_id", "value": "acme-corp"}])])
        with mock.patch.object(lf.random, "random", return_value=0.0):  # always keep
            out = lf.lambda_handler(firehose(json.dumps(event())))["records"]
        self.assertEqual(json.loads(body_of(out[0]))["weight"], 2)

    def test_only_matching_events_leave_a_batched_record(self):
        batched = "\n".join([json.dumps(event()),
                             json.dumps(event(company_id="globex")),
                             json.dumps(event())]) + "\n"
        out = lf.lambda_handler(firehose(batched))["records"][0]
        self.assertEqual(out["result"], "Ok")
        lines = [json.loads(l) for l in body_of(out).strip().split("\n")]
        self.assertEqual([e["company_id"] for e in lines], ["globex"])

    def test_json_arrays_stay_arrays(self):
        out = lf.lambda_handler(firehose(json.dumps([event(), event(company_id="globex")])))["records"][0]
        self.assertEqual([e["company_id"] for e in json.loads(body_of(out))], ["globex"])

    def test_untouched_records_pass_through_byte_identical(self):
        original = '{ "company_id" : "globex" }'
        payload = firehose(original)
        out = lf.lambda_handler(payload)["records"][0]
        self.assertEqual(out["data"], payload["records"][0]["data"])

    def test_unreadable_records_pass_through(self):
        out = lf.lambda_handler({"records": [{"recordId": "r", "data": "!!!not base64!!!"}]})
        self.assertEqual(out["records"][0]["result"], "Ok")
        out = lf.lambda_handler(firehose("plain text log line"))["records"][0]
        self.assertEqual(body_of(out), "plain text log line")

    def test_empty_batch(self):
        self.assertEqual(lf.lambda_handler({"records": []}), {"records": []})


class TestKeepEverythingFallback(unittest.TestCase):
    """With no rules from Moesif, nothing is sampled."""

    def setUp(self):
        lf._CONFIG = None
        os.environ.pop("MOESIF_APPLICATION_ID", None)

    def tearDown(self):
        lf._CONFIG = None
        os.environ.pop("MOESIF_APPLICATION_ID", None)
        os.environ.pop("CONFIG_FETCH_TIMEOUT_SECONDS", None)

    def test_no_application_id_means_no_sampling(self):
        cfg = lf.get_config()
        self.assertEqual(cfg["default_sample_rate"], 100.0)
        self.assertEqual(cfg["rules"], [])
        self.assertTrue(lf.samples_everything(cfg))

    def test_the_fallback_is_flagged_as_not_configured(self):
        cfg = lf.get_config()
        self.assertFalse(cfg["valid"])
        self.assertIn("MOESIF_APPLICATION_ID", cfg["source"])

    def test_no_application_id_makes_no_network_call(self):
        with mock.patch.object(lf.urllib.request, "urlopen") as opened:
            lf.get_config()
        opened.assert_not_called()

    def test_every_event_passes_through(self):
        out = lf.lambda_handler(firehose(json.dumps(event()), json.dumps(event())))
        self.assertEqual([r["result"] for r in out["records"]], ["Ok", "Ok"])

    def test_an_unreachable_moesif_means_no_sampling(self):
        os.environ["MOESIF_APPLICATION_ID"] = "test-app-id"
        os.environ["CONFIG_FETCH_TIMEOUT_SECONDS"] = "0.01"
        os.environ["CONFIG_FETCH_RETRIES"] = "0"   # retries are exercised separately
        with mock.patch.object(lf.urllib.request, "urlopen", side_effect=OSError("down")):
            cfg = lf.get_config()
        self.assertTrue(lf.samples_everything(cfg))
        self.assertFalse(cfg["valid"])
        self.assertIn("unavailable", cfg["source"])


class TestPathLookup(unittest.TestCase):
    def test_reads_nested_and_top_level_values(self):
        self.assertEqual(lf.get_path(event(), "response.status"), 200)
        self.assertEqual(lf.get_path(event(), "company_id"), "acme-corp")

    def test_absent_path_returns_the_missing_sentinel(self):
        self.assertIs(lf.get_path(event(), "response.latency"), lf.MISSING)

    def test_missing_is_distinct_from_a_json_null(self):
        self.assertIsNone(lf.get_path({"user_id": None}, "user_id"))
        self.assertIs(lf.get_path({}, "user_id"), lf.MISSING)

    def test_exact_key_wins_over_the_case_insensitive_fallback(self):
        payload = {"id": "lower", "ID": "upper"}
        self.assertEqual(lf.get_path(payload, "id"), "lower")
        self.assertEqual(lf.get_path(payload, "ID"), "upper")

    def test_every_segment_can_match_case_insensitively(self):
        self.assertEqual(lf.get_path(event(), "REQUEST.HEADERS.X-CLIENT-ID"), "acme-corp")

    def test_indexes_into_lists(self):
        payload = {"items": [{"id": "a"}, {"id": "b"}]}
        self.assertEqual(lf.get_path(payload, "items.1.id"), "b")
        self.assertEqual(lf.get_path(payload, "items.-1.id"), "b")

    def test_out_of_range_and_non_numeric_indexes_are_missing(self):
        self.assertIs(lf.get_path({"items": [1]}, "items.5"), lf.MISSING)
        self.assertIs(lf.get_path({"items": [1]}, "items.id"), lf.MISSING)

    def test_descending_into_a_scalar_is_missing(self):
        self.assertIs(lf.get_path({"status": 200}, "status.code"), lf.MISSING)


class TestEveryOperator(unittest.TestCase):
    """Each operator, in both directions, against one known event."""

    EVENT = {"company_id": "Acme-Corp", "user_id": None, "tier": "gold",
             "request": {"verb": "GET", "uri": "https://api.x.com/v1/items?page=2"},
             "response": {"status": 404}}

    def check(self, expected, **condition):
        result = lf._matches(lf._parse_condition(condition), self.EVENT)
        self.assertEqual(result, expected, condition)

    def test_equals(self):
        self.check(True, path="response.status", value=404)
        self.check(True, path="response.status", value="404")     # string coerces
        self.check(False, path="response.status", value=200)
        self.check(False, path="company_id", value="acme-corp")   # case sensitive
        self.check(True, path="company_id", value="acme-corp", ignore_case=True)

    def test_not_equals(self):
        self.check(True, path="request.verb", operator="not_equals", value="POST")
        self.check(False, path="request.verb", operator="not_equals", value="GET")

    def test_in(self):
        self.check(True, path="request.verb", operator="in", value=["GET", "HEAD"])
        self.check(False, path="request.verb", operator="in", value=["POST"])
        self.check(True, path="request.verb", operator="in", value=["get"], ignore_case=True)

    def test_not_in(self):
        self.check(True, path="request.verb", operator="not_in", value=["POST", "PUT"])
        self.check(False, path="request.verb", operator="not_in", value=["GET"])

    def test_regex(self):
        self.check(True, path="request.route", operator="regex", value="^/v1/")
        self.check(False, path="request.route", operator="regex", value="^/v2/")
        self.check(True, path="request.uri", operator="regex", value="PAGE=2", ignore_case=True)

    def test_contains(self):
        self.check(True, path="request.uri", operator="contains", value="page=2")
        self.check(False, path="request.uri", operator="contains", value="page=9")
        self.check(True, path="company_id", operator="contains", value="ACME", ignore_case=True)

    def test_numeric_comparisons(self):
        self.check(True, path="response.status", operator="gt", value=400)
        self.check(False, path="response.status", operator="gt", value=404)
        self.check(True, path="response.status", operator="gte", value=404)
        self.check(True, path="response.status", operator="lt", value=500)
        self.check(False, path="response.status", operator="lt", value=404)
        self.check(True, path="response.status", operator="lte", value=404)

    def test_numeric_comparison_against_text_does_not_match(self):
        self.check(False, path="tier", operator="gt", value=100)

    def test_between(self):
        self.check(True, path="response.status", operator="between", value=[400, 499])
        self.check(False, path="response.status", operator="between", value=[200, 299])
        self.check(True, path="response.status", operator="between", value=[404, 404])
        self.check(True, path="response.status", operator="between", value=[499, 400])  # reversed

    def test_exists_and_not_exists(self):
        self.check(True, path="company_id", operator="exists")
        self.check(False, path="user_id", operator="exists")        # null counts as absent
        self.check(False, path="nope", operator="exists")
        self.check(True, path="user_id", operator="not_exists")
        self.check(False, path="company_id", operator="not_exists")

    def test_every_operator_is_covered_by_a_test(self):
        tested = {"equals", "not_equals", "in", "not_in", "regex", "contains",
                  "gt", "gte", "lt", "lte", "between", "exists", "not_exists"}
        self.assertEqual(tested, set(lf.OPERATORS))


class TestConditionValidation(unittest.TestCase):
    def bad(self, **condition):
        with self.assertRaises(Exception):
            lf._parse_condition(condition)

    def test_rejects_unknown_operator(self):
        self.bad(path="a", operator="approximately", value=1)

    def test_rejects_missing_or_empty_path(self):
        self.bad(operator="equals", value=1)
        self.bad(path="", value=1)

    def test_rejects_uncompilable_regex(self):
        self.bad(path="a", operator="regex", value="(")

    def test_defaults_to_equals(self):
        self.assertEqual(lf._parse_condition({"path": "a", "value": 1})["operator"], "equals")

    def test_compiles_the_regex_once_at_load(self):
        parsed = lf._parse_condition({"path": "a", "operator": "regex", "value": "^/v1"})
        self.assertTrue(hasattr(parsed["regex"], "search"))


class TestRuleValidation(unittest.TestCase):
    def rule_raises(self, raw):
        with self.assertRaises(Exception):
            lf._parse_rule(raw, 0)

    def test_rejects_a_rule_with_no_conditions(self):
        self.rule_raises({"name": "catch-all", "sample_rate": 10, "conditions": []})
        self.rule_raises({"name": "catch-all", "sample_rate": 10})

    def test_rejects_rates_outside_0_to_100(self):
        for bad in (-1, 101, 500):
            self.rule_raises({"sample_rate": bad, "conditions": [{"path": "a", "value": 1}]})

    def test_rejects_non_numeric_and_boolean_rates(self):
        for bad in ("10%", None, True, [10]):
            self.rule_raises({"sample_rate": bad, "conditions": [{"path": "a", "value": 1}]})

    def test_accepts_the_boundary_rates(self):
        for good in (0, 100, 0.5):
            parsed = lf._parse_rule({"sample_rate": good, "conditions": [{"path": "a", "value": 1}]}, 0)
            self.assertEqual(parsed["sample_rate"], float(good))

    def test_names_an_unnamed_rule_by_index(self):
        parsed = lf._parse_rule({"sample_rate": 10, "conditions": [{"path": "a", "value": 1}]}, 3)
        self.assertEqual(parsed["name"], "rule[3]")


class TestRouteDerivation(unittest.TestCase):
    def test_strips_scheme_host_and_query(self):
        self.assertEqual(lf._route({"request": {"uri": "https://api.x.com/v1/items?page=2"}}), "/v1/items")

    def test_http_as_well_as_https(self):
        self.assertEqual(lf._route({"request": {"uri": "http://api.x.com/v1/items"}}), "/v1/items")

    def test_a_url_with_no_path_becomes_slash(self):
        self.assertEqual(lf._route({"request": {"uri": "https://api.x.com"}}), "/")

    def test_a_relative_uri_becomes_slash(self):
        # The pattern requires scheme and host, so a relative URI yields '/'.
        self.assertEqual(lf._route({"request": {"uri": "/v1/items"}}), "/")

    def test_a_non_string_uri_is_missing(self):
        self.assertIs(lf._route({"request": {"uri": 42}}), lf.MISSING)
        self.assertIs(lf._route({}), lf.MISSING)


class TestRecordShapes(unittest.TestCase):
    def setUp(self):
        lf._CONFIG = config([rule("drop acme", 0, [{"path": "company_id", "value": "acme-corp"}])])

    def tearDown(self):
        lf._CONFIG = None

    def run_one(self, body):
        return lf.lambda_handler(firehose(body))["records"][0]

    def test_pretty_printed_json_is_one_event_not_many_lines(self):
        out = self.run_one(json.dumps(event(), indent=2))
        self.assertEqual(out["result"], "Dropped")

    def test_ndjson_without_a_trailing_newline(self):
        body = json.dumps(event(company_id="globex")) + "\n" + json.dumps(event())
        out = self.run_one(body)
        self.assertEqual(out["result"], "Ok")
        self.assertFalse(body_of(out).endswith("\n"))

    def test_a_trailing_newline_is_preserved(self):
        body = json.dumps(event(company_id="globex")) + "\n" + json.dumps(event()) + "\n"
        self.assertTrue(body_of(self.run_one(body)).endswith("\n"))

    def test_blank_body_passes_through(self):
        self.assertEqual(self.run_one("   ")["result"], "Ok")

    def test_bare_scalars_are_not_treated_as_events(self):
        for body in ("42", '"a string"', "true"):
            self.assertEqual(self.run_one(body)["result"], "Ok", body)

    def test_an_array_with_every_event_dropped_drops_the_record(self):
        self.assertEqual(self.run_one(json.dumps([event(), event()]))["result"], "Dropped")

    def test_a_null_inside_an_array_survives(self):
        body = json.dumps([None, event(company_id="globex")])
        self.assertIn(None, json.loads(body_of(self.run_one(body))))

    def test_mixed_valid_and_invalid_lines(self):
        body = "\n".join(["not json", json.dumps(event()), json.dumps(event(company_id="globex"))])
        text = body_of(self.run_one(body))
        self.assertIn("not json", text)          # unparseable line kept verbatim
        companies = [json.loads(l)["company_id"] for l in text.split("\n") if l.startswith("{")]
        self.assertEqual(companies, ["globex"])  # acme dropped, globex kept


class TestHandlerEdgeCases(unittest.TestCase):
    def setUp(self):
        lf._CONFIG = config([rule("drop acme", 0, [{"path": "company_id", "value": "acme-corp"}])])

    def tearDown(self):
        lf._CONFIG = None

    def test_a_record_with_no_data_key_passes_through(self):
        out = lf.lambda_handler({"records": [{"recordId": "r"}]})["records"][0]
        self.assertEqual(out["result"], "Ok")

    def test_a_record_with_no_record_id_does_not_crash(self):
        out = lf.lambda_handler({"records": [{"data": "!!!"}]})["records"][0]
        self.assertEqual(out["result"], "Ok")

    def test_missing_records_key(self):
        self.assertEqual(lf.lambda_handler({}), {"records": []})

    def test_a_keep_everything_config_skips_decoding(self):
        lf._CONFIG = config(default=100)
        out = lf.lambda_handler({"records": [{"recordId": "r", "data": "!!!not base64!!!"}]})
        self.assertEqual(out["records"][0]["data"], "!!!not base64!!!")

    def test_order_is_preserved_across_a_large_batch(self):
        ids = ["r%d" % i for i in range(50)]
        payload = {"records": [
            {"recordId": i, "data": base64.b64encode(json.dumps(event()).encode()).decode()}
            for i in ids]}
        self.assertEqual([r["recordId"] for r in lf.lambda_handler(payload)["records"]], ids)

    def test_a_crash_inside_processing_passes_the_record_through(self):
        with mock.patch.object(lf, "_split", side_effect=RuntimeError("boom")):
            out = lf.lambda_handler(firehose(json.dumps(event())))["records"][0]
        self.assertEqual(out["result"], "Ok")
        self.assertIsNotNone(out["data"])


class TestSummaryLog(unittest.TestCase):
    def tearDown(self):
        lf._CONFIG = None

    def test_counters_add_up(self):
        lf._CONFIG = config([rule("drop acme", 0, [{"path": "company_id", "value": "acme-corp"}])])
        payload = firehose(json.dumps(event()),
                           json.dumps(event(company_id="globex")),
                           json.dumps(event()))
        with self.assertLogs(level="INFO") as captured:
            lf.lambda_handler(payload)

        summaries = [json.loads(r.getMessage()) for r in captured.records
                     if r.getMessage().startswith("{")
                     and "firehose_sampling_summary" in r.getMessage()]
        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertEqual(summary["records_in"], 3)
        self.assertEqual(summary["events_in"], 3)
        self.assertEqual(summary["events_kept"], 1)
        self.assertEqual(summary["events_dropped"], 2)
        self.assertEqual(summary["records_dropped"], 2)
        self.assertTrue(summary["config_valid"])
        self.assertEqual(summary["events_kept"] + summary["events_dropped"], summary["events_in"])


class TestSamplingDistribution(unittest.TestCase):
    """The statistical guarantees, over enough trials that noise cannot hide a bug."""

    def test_each_rate_lands_near_its_target(self):
        for rate, tolerance in [(50, 3), (25, 2), (10, 1.5), (5, 1), (1, 0.5)]:
            random.seed(99)
            kept = sum(lf.should_keep(rate) for _ in range(20000))
            actual = 100.0 * kept / 20000
            self.assertAlmostEqual(actual, rate, delta=tolerance,
                                   msg="rate %s produced %.2f%%" % (rate, actual))

    def test_weighted_totals_reconstruct_volume_at_every_rate(self):
        for rate in (50, 25, 20, 10, 5):
            random.seed(5)
            cfg = config([rule("r", rate, [{"path": "company_id", "value": "acme-corp"}])])
            total = 0
            for _ in range(20000):
                payload = event()
                if lf.sample(payload, cfg)[0]:
                    total += payload.get("weight", 1)
            self.assertAlmostEqual(total, 20000, delta=2000,
                                   msg="rate %s extrapolated to %s" % (rate, total))

    def test_rate_zero_keeps_nothing_and_rate_100_keeps_everything(self):
        random.seed(1)
        self.assertEqual(sum(lf.should_keep(0) for _ in range(1000)), 0)
        self.assertEqual(sum(lf.should_keep(100) for _ in range(1000)), 1000)



MOESIF_CONFIG = {
    "org_id": "org-1",
    "app_id": "app-1",
    "sample_rate": 80,
    "user_sample_rate": {"u-1001": 10},
    "company_sample_rate": {"acme-corp": 25},
    "regex_config": [
        {"sample_rate": 5,
         "conditions": [{"path": "request.route", "value": "^/v1/items"},
                        {"path": "request.verb", "value": "GET"}]}
    ],
}


class FakeResponse:
    """Stands in for the object urlopen returns."""

    def __init__(self, body, etag=None):
        self._body = json.dumps(body).encode()
        self.headers = {"X-Moesif-Config-ETag": etag} if etag else {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code):
    return lf.urllib.error.HTTPError("http://x", code, "err", {}, None)


class FakeClock:
    """A clock that only advances when the code under test sleeps."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class DynamicConfigBase(unittest.TestCase):
    def setUp(self):
        lf._CONFIG = None
        lf._ETAG = None
        lf._FETCHED_AT = 0.0
        os.environ["MOESIF_APPLICATION_ID"] = "test-app-id"
        # A tiny budget keeps unrelated tests to a single attempt.
        os.environ["CONFIG_FETCH_TIMEOUT_SECONDS"] = "0.01"

    def tearDown(self):
        lf._CONFIG = None
        lf._ETAG = None
        lf._FETCHED_AT = 0.0
        for key in ("MOESIF_APPLICATION_ID", "MOESIF_BASE_URI", "CONFIG_REFRESH_SECONDS",
                    "CONFIG_FETCH_TIMEOUT_SECONDS"):
            os.environ.pop(key, None)


class TestMoesifConfigTranslation(DynamicConfigBase):
    def test_regex_config_becomes_rules(self):
        cfg = lf._from_moesif_config(MOESIF_CONFIG)
        self.assertEqual(len(cfg["rules"]), 1)
        rule = cfg["rules"][0]
        self.assertEqual(rule["sample_rate"], 5.0)
        self.assertTrue(all(c["operator"] == "regex" for c in rule["conditions"]))

    def test_sample_rate_becomes_the_default(self):
        self.assertEqual(lf._from_moesif_config(MOESIF_CONFIG)["default_sample_rate"], 80.0)

    def test_user_and_company_maps_are_carried_over(self):
        cfg = lf._from_moesif_config(MOESIF_CONFIG)
        self.assertEqual(cfg["user_sample_rate"], {"u-1001": 10.0})
        self.assertEqual(cfg["company_sample_rate"], {"acme-corp": 25.0})

    def test_an_empty_config_is_keep_everything(self):
        cfg = lf._from_moesif_config({})
        self.assertEqual(cfg["default_sample_rate"], 100.0)
        self.assertTrue(lf.samples_everything(cfg))

    def test_one_bad_regex_entry_does_not_lose_the_rest(self):
        raw = dict(MOESIF_CONFIG, regex_config=[
            {"sample_rate": 500, "conditions": [{"path": "a", "value": "b"}]},   # bad rate
            {"sample_rate": 5, "conditions": [{"path": "request.verb", "value": "GET"}]},
        ])
        cfg = lf._from_moesif_config(raw)
        self.assertEqual(len(cfg["rules"]), 1)

    def test_an_invalid_document_raises_so_the_caller_can_fall_back(self):
        with self.assertRaises(Exception):
            lf._from_moesif_config([1, 2, 3])


class TestRatePrecedence(DynamicConfigBase):
    """Rules, then user, then company, then default."""

    def setUp(self):
        super().setUp()
        self.cfg = lf._from_moesif_config(MOESIF_CONFIG)

    def rate_for(self, **overrides):
        e = {"user_id": "u-9", "company_id": "other",
             "request": {"verb": "POST", "uri": "https://api.x.com/v1/other"},
             "response": {"status": 200}}
        e.update(overrides)
        return lf.resolve_rate(e, self.cfg)

    def test_regex_rule_beats_user_and_company(self):
        rate, name = self.rate_for(user_id="u-1001", company_id="acme-corp",
                                   request={"verb": "GET", "uri": "https://api.x.com/v1/items"})
        self.assertEqual(rate, 5.0)
        self.assertTrue(name.startswith("regex_config"))

    def test_user_beats_company(self):
        rate, name = self.rate_for(user_id="u-1001", company_id="acme-corp")
        self.assertEqual((rate, name), (10.0, "user:u-1001"))

    def test_company_applies_when_the_user_has_no_rate(self):
        rate, name = self.rate_for(user_id="u-9", company_id="acme-corp")
        self.assertEqual((rate, name), (25.0, "company:acme-corp"))

    def test_default_when_nothing_matches(self):
        self.assertEqual(self.rate_for()[0], 80.0)

    def test_a_null_user_id_does_not_match_a_rate_map(self):
        self.assertEqual(self.rate_for(user_id=None, company_id="acme-corp")[1], "company:acme-corp")


class TestRemoteFetch(DynamicConfigBase):
    def test_returns_none_without_an_application_id(self):
        os.environ.pop("MOESIF_APPLICATION_ID")
        self.assertIsNone(lf.fetch_remote_config())

    def test_sends_the_application_id_and_reads_the_etag(self):
        with mock.patch.object(lf.urllib.request, "urlopen",
                               return_value=FakeResponse(MOESIF_CONFIG, "etag-1")) as opened:
            cfg = lf.fetch_remote_config()

        request = opened.call_args[0][0]
        self.assertEqual(request.get_full_url(), "https://api.moesif.net/v1/config")
        self.assertEqual(request.get_header("X-moesif-application-id"), "test-app-id")
        self.assertEqual(lf._ETAG, "etag-1")
        self.assertEqual(cfg["default_sample_rate"], 80.0)

    def test_base_uri_is_configurable(self):
        os.environ["MOESIF_BASE_URI"] = "https://api.moesif.com/"
        with mock.patch.object(lf.urllib.request, "urlopen",
                               return_value=FakeResponse(MOESIF_CONFIG)) as opened:
            lf.fetch_remote_config()
        self.assertEqual(opened.call_args[0][0].get_full_url(), "https://api.moesif.com/v1/config")

    def test_a_known_etag_is_sent_back_as_if_none_match(self):
        lf._ETAG = "etag-1"
        with mock.patch.object(lf.urllib.request, "urlopen",
                               return_value=FakeResponse(MOESIF_CONFIG, "etag-2")) as opened:
            lf.fetch_remote_config()
        self.assertEqual(opened.call_args[0][0].get_header("If-none-match"), "etag-1")

    def test_304_keeps_the_current_config(self):
        with mock.patch.object(lf.urllib.request, "urlopen", side_effect=http_error(304)):
            self.assertIsNone(lf.fetch_remote_config())

    def test_unauthorized_is_reported_and_does_not_raise(self):
        with mock.patch.object(lf.urllib.request, "urlopen", side_effect=http_error(401)):
            with self.assertLogs(level="ERROR") as captured:
                self.assertIsNone(lf.fetch_remote_config())
        self.assertIn("MOESIF_APPLICATION_ID", "".join(captured.output))

    def test_a_network_failure_does_not_raise(self):
        with mock.patch.object(lf.urllib.request, "urlopen", side_effect=OSError("timeout")):
            self.assertIsNone(lf.fetch_remote_config())

    def test_a_malformed_body_does_not_raise(self):
        broken = mock.MagicMock()
        broken.__enter__ = lambda self: self
        broken.__exit__ = lambda self, *a: False
        broken.read = lambda: b"{not json"
        broken.headers = {}
        with mock.patch.object(lf.urllib.request, "urlopen", return_value=broken):
            self.assertIsNone(lf.fetch_remote_config())


class TestGetConfig(DynamicConfigBase):
    def test_cold_start_prefers_moesif(self):
        with mock.patch.object(lf.urllib.request, "urlopen",
                               return_value=FakeResponse(MOESIF_CONFIG, "etag-1")):
            cfg = lf.get_config()
        self.assertEqual(cfg["source"], "moesif:/v1/config")
        self.assertEqual(cfg["default_sample_rate"], 80.0)

    def test_a_failed_cold_start_samples_nothing_until_the_next_refresh(self):
        with mock.patch.object(lf.urllib.request, "urlopen", side_effect=OSError("down")):
            cfg = lf.get_config()
        self.assertTrue(lf.samples_everything(cfg))

        lf._FETCHED_AT -= 3600
        with mock.patch.object(lf.urllib.request, "urlopen",
                               return_value=FakeResponse(MOESIF_CONFIG, "etag-1")):
            cfg = lf.get_config()
        self.assertEqual(cfg["default_sample_rate"], 80.0)

    def test_the_config_is_cached_between_invocations(self):
        os.environ["CONFIG_REFRESH_SECONDS"] = "3600"
        with mock.patch.object(lf.urllib.request, "urlopen",
                               return_value=FakeResponse(MOESIF_CONFIG, "etag-1")):
            first = lf.get_config()
            second = lf.get_config()
        self.assertIs(first, second)

    def test_it_does_not_refetch_before_the_interval_elapses(self):
        os.environ["CONFIG_REFRESH_SECONDS"] = "3600"
        with mock.patch.object(lf.urllib.request, "urlopen",
                               return_value=FakeResponse(MOESIF_CONFIG, "etag-1")) as opened:
            lf.get_config()
            lf.get_config()
            lf.get_config()
        self.assertEqual(opened.call_count, 1)

    def test_it_refetches_once_the_interval_has_elapsed(self):
        os.environ["CONFIG_REFRESH_SECONDS"] = "60"
        with mock.patch.object(lf.urllib.request, "urlopen",
                               return_value=FakeResponse(MOESIF_CONFIG, "etag-1")) as opened:
            lf.get_config()
            lf._FETCHED_AT -= 61          # pretend a minute passed
            lf.get_config()
        self.assertEqual(opened.call_count, 2)

    def test_a_failed_refresh_keeps_the_config_already_in_use(self):
        with mock.patch.object(lf.urllib.request, "urlopen",
                               return_value=FakeResponse(MOESIF_CONFIG, "etag-1")):
            first = lf.get_config()
        lf._FETCHED_AT -= 3600
        with mock.patch.object(lf.urllib.request, "urlopen", side_effect=OSError("down")):
            second = lf.get_config()
        self.assertIs(second, first)
        self.assertEqual(second["default_sample_rate"], 80.0)

    def test_a_failing_endpoint_is_not_retried_on_every_invocation(self):
        os.environ["CONFIG_REFRESH_SECONDS"] = "3600"
        with mock.patch.object(lf.urllib.request, "urlopen", side_effect=OSError("down")) as opened:
            lf.get_config()
            lf.get_config()
            lf.get_config()
        self.assertEqual(opened.call_count, 1)

    def test_the_handler_reports_where_the_config_came_from(self):
        with mock.patch.object(lf.urllib.request, "urlopen",
                               return_value=FakeResponse(MOESIF_CONFIG, "etag-1")):
            with self.assertLogs(level="INFO") as captured:
                lf.lambda_handler({"invocationId": "x", "records": []})
        summaries = [json.loads(r.getMessage()) for r in captured.records
                     if r.getMessage().startswith("{")
                     and "firehose_sampling_summary" in r.getMessage()]
        self.assertEqual(summaries[0]["config_source"], "moesif:/v1/config")


class TestSamplesEverything(unittest.TestCase):
    def test_true_only_when_nothing_can_drop(self):
        self.assertTrue(lf.samples_everything(config()))
        self.assertFalse(lf.samples_everything(config(default=50)))
        self.assertFalse(lf.samples_everything(
            config(rules=[rule("r", 10, [{"path": "a", "value": 1}])])))
        self.assertFalse(lf.samples_everything(config(user_sample_rate={"u-1": 10.0})))
        self.assertFalse(lf.samples_everything(config(company_sample_rate={"c-1": 10.0})))



class TestDebugSetting(unittest.TestCase):
    """DEBUG switches verbose logging on and off."""

    def tearDown(self):
        import importlib
        os.environ.pop("DEBUG", None)
        importlib.reload(lf)

    def reload_with(self, value):
        import importlib
        if value is None:
            os.environ.pop("DEBUG", None)
        else:
            os.environ["DEBUG"] = value
        return importlib.reload(lf)

    def test_off_by_default(self):
        import logging
        self.assertEqual(logging.getLevelName(self.reload_with(None).logger.level), "INFO")

    def test_truthy_values_enable_debug(self):
        import logging
        for value in ("true", "True", "TRUE", "1", "yes", "on"):
            self.assertEqual(logging.getLevelName(self.reload_with(value).logger.level),
                             "DEBUG", value)

    def test_falsy_and_unusable_values_stay_at_info(self):
        import logging
        for value in ("false", "0", "no", "off", "", "   ", "maybe"):
            self.assertEqual(logging.getLevelName(self.reload_with(value).logger.level),
                             "INFO", repr(value))



class TestFetchSettings(DynamicConfigBase):
    """The tunables that control the call itself."""

    def urlopen_kwargs(self):
        with mock.patch.object(lf.urllib.request, "urlopen",
                               return_value=FakeResponse(MOESIF_CONFIG)) as opened:
            lf.fetch_remote_config()
        return opened.call_args

    def test_the_default_budget_is_six_seconds(self):
        os.environ.pop("CONFIG_FETCH_TIMEOUT_SECONDS", None)
        self.assertEqual(lf._fetch_timeout(), 6.0)

    def test_the_budget_is_configurable(self):
        os.environ["CONFIG_FETCH_TIMEOUT_SECONDS"] = "0.5"
        self.assertEqual(lf._fetch_timeout(), 0.5)

    def test_an_unusable_budget_falls_back(self):
        os.environ["CONFIG_FETCH_TIMEOUT_SECONDS"] = "soon"
        self.assertEqual(lf._fetch_timeout(), 6.0)

    def test_the_first_attempt_is_given_the_budget(self):
        os.environ["CONFIG_FETCH_TIMEOUT_SECONDS"] = "0.5"
        self.assertAlmostEqual(self.urlopen_kwargs().kwargs["timeout"], 0.5, places=2)

    def test_the_refresh_interval_is_configurable(self):
        os.environ["CONFIG_REFRESH_SECONDS"] = "300"
        self.assertEqual(lf._refresh_seconds(), 300.0)

    def test_an_unusable_refresh_interval_falls_back_to_sixty(self):
        os.environ["CONFIG_REFRESH_SECONDS"] = "often"
        self.assertEqual(lf._refresh_seconds(), 60.0)

    def test_a_negative_refresh_interval_is_clamped(self):
        os.environ["CONFIG_REFRESH_SECONDS"] = "-10"
        self.assertEqual(lf._refresh_seconds(), 0.0)

    def test_a_response_without_an_etag_keeps_the_previous_one(self):
        lf._ETAG = "etag-1"
        with mock.patch.object(lf.urllib.request, "urlopen",
                               return_value=FakeResponse(MOESIF_CONFIG)):
            lf.fetch_remote_config()
        self.assertEqual(lf._ETAG, "etag-1")


class TestDynamicConfigEndToEnd(DynamicConfigBase):
    """A config fetched from Moesif actually samples the batch."""

    def run_batch(self, config_document, bodies):
        with mock.patch.object(lf.urllib.request, "urlopen",
                               return_value=FakeResponse(config_document, "etag-1")):
            return lf.lambda_handler(firehose(*bodies))["records"]

    def test_a_regex_rule_from_moesif_drops_records(self):
        document = {"sample_rate": 100, "regex_config": [
            {"sample_rate": 0, "conditions": [{"path": "request.route", "value": "^/health$"}]}]}
        results = self.run_batch(document, [
            json.dumps(event(request={"verb": "GET", "uri": "https://api.x.com/health"})),
            json.dumps(event()),
        ])
        self.assertEqual([r["result"] for r in results], ["Dropped", "Ok"])

    def test_a_company_rate_from_moesif_drops_records(self):
        document = {"sample_rate": 100, "company_sample_rate": {"acme-corp": 0}}
        results = self.run_batch(document, [
            json.dumps(event()),                             # acme-corp
            json.dumps(event(company_id="globex")),
        ])
        self.assertEqual([r["result"] for r in results], ["Dropped", "Ok"])

    def test_a_user_rate_from_moesif_stamps_weight(self):
        document = {"sample_rate": 100, "user_sample_rate": {"u-1": 50}}
        with mock.patch.object(lf.random, "random", return_value=0.0):   # always keep
            results = self.run_batch(document, [json.dumps(event(user_id="u-1"))])
        self.assertEqual(json.loads(body_of(results[0]))["weight"], 2)

    def test_a_company_rate_from_moesif_stamps_weight(self):
        document = {"sample_rate": 100, "company_sample_rate": {"acme-corp": 25}}
        with mock.patch.object(lf.random, "random", return_value=0.0):
            results = self.run_batch(document, [json.dumps(event())])
        self.assertEqual(json.loads(body_of(results[0]))["weight"], 4)

    def test_a_regex_rule_from_moesif_stamps_weight(self):
        document = {"sample_rate": 100, "regex_config": [
            {"sample_rate": 10, "conditions": [{"path": "request.verb", "value": "^GET$"}]}]}
        with mock.patch.object(lf.random, "random", return_value=0.0):
            results = self.run_batch(document, [json.dumps(event())])
        self.assertEqual(json.loads(body_of(results[0]))["weight"], 10)

    def test_every_event_in_a_batched_record_is_weighted(self):
        document = {"sample_rate": 100, "company_sample_rate": {"acme-corp": 50}}
        batched = "\n".join(json.dumps(event()) for _ in range(3))
        with mock.patch.object(lf.random, "random", return_value=0.0):
            results = self.run_batch(document, [batched])
        weights = [json.loads(line)["weight"]
                   for line in body_of(results[0]).strip().split("\n")]
        self.assertEqual(weights, [2, 2, 2])

    def test_ip_address_is_matchable_like_the_other_fields(self):
        document = {"sample_rate": 100, "regex_config": [
            {"sample_rate": 0,
             "conditions": [{"path": "request.ip_address", "value": "^10\\."}]}]}
        results = self.run_batch(document, [
            json.dumps(event(request={"verb": "GET", "uri": "https://api.x.com/v1/items",
                                      "ip_address": "10.0.0.1"})),
            json.dumps(event(request={"verb": "GET", "uri": "https://api.x.com/v1/items",
                                      "ip_address": "203.0.113.5"})),
        ])
        self.assertEqual([r["result"] for r in results], ["Dropped", "Ok"])




class TestFetchBudget(DynamicConfigBase):
    """Retries continue until the time budget is spent."""

    def setUp(self):
        super().setUp()
        os.environ["CONFIG_FETCH_TIMEOUT_SECONDS"] = "6"

    def attempts(self, side_effect):
        """Run a fetch against a clock that only moves when the code backs off."""
        clock = FakeClock()
        with mock.patch.object(lf.time, "sleep", clock.sleep), \
             mock.patch.object(lf.time, "monotonic", clock.monotonic):
            with mock.patch.object(lf.urllib.request, "urlopen",
                                   side_effect=side_effect) as opened:
                result = lf.fetch_remote_config()
        return opened.call_count, result, clock.now

    def test_failures_are_retried_until_the_budget_runs_out(self):
        count, result, elapsed = self.attempts(TimeoutError("read timed out"))
        self.assertGreater(count, 1)
        self.assertIsNone(result)
        self.assertLessEqual(elapsed, 6.0)

    def test_every_failure_kind_is_retried(self):
        for side_effect in (TimeoutError("slow"), OSError("refused"),
                            http_error(401), http_error(404), http_error(503)):
            count, _, _ = self.attempts(side_effect)
            self.assertGreater(count, 1, repr(side_effect))

    def test_it_stops_as_soon_as_one_attempt_succeeds(self):
        count, result, _ = self.attempts(
            [TimeoutError("slow"), FakeResponse(MOESIF_CONFIG, "etag-1")])
        self.assertEqual(count, 2)
        self.assertEqual(result["default_sample_rate"], 80.0)

    def test_304_is_not_retried(self):
        # An unchanged config is a success, not a failure.
        count, _, _ = self.attempts(http_error(304))
        self.assertEqual(count, 1)

    def test_each_attempt_gets_only_the_time_that_is_left(self):
        timeouts = []
        clock = FakeClock()

        def record(request, timeout=None):
            timeouts.append(timeout)
            raise TimeoutError("slow")

        with mock.patch.object(lf.time, "sleep", clock.sleep), \
             mock.patch.object(lf.time, "monotonic", clock.monotonic):
            with mock.patch.object(lf.urllib.request, "urlopen", side_effect=record):
                lf.fetch_remote_config()

        self.assertEqual(timeouts, sorted(timeouts, reverse=True))  # shrinking
        self.assertLessEqual(timeouts[0], 6.0)
        self.assertGreater(timeouts[-1], 0)

    def test_a_small_budget_allows_one_attempt(self):
        os.environ["CONFIG_FETCH_TIMEOUT_SECONDS"] = "0.01"
        count, _, _ = self.attempts(TimeoutError("slow"))
        self.assertEqual(count, 1)

    def test_an_unusable_budget_falls_back_to_six(self):
        os.environ["CONFIG_FETCH_TIMEOUT_SECONDS"] = "soon"
        self.assertEqual(lf._fetch_timeout(), 6.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
