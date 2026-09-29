"""Tests for the sampling logic.

Run from the project root: python3 -m unittest discover -s tests -v
"""

import base64
import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# The function lives at the project root, one level up from this file.
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


def config(rules=(), default=100):
    return {"default_sample_rate": float(default), "rules": list(rules), "valid": True}


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
        # Matches the SDK, so an anchored rule ports across unchanged.
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
        # Matches the SDKs, which assign weight unconditionally.
        payload = event(weight=10)
        lf.stamp_weight(payload, 50)
        self.assertEqual(payload["weight"], 2)

    def test_a_stale_weight_is_corrected_even_at_full_rate(self):
        payload = event(weight=10)
        lf.stamp_weight(payload, 100)
        self.assertEqual(payload["weight"], 1)

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


class TestConfigLoading(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("SAMPLING_CONFIG", None)
        lf._CONFIG = None

    def test_reads_inline_json(self):
        os.environ["SAMPLING_CONFIG"] = json.dumps({"default_sample_rate": 25, "rules": []})
        cfg = lf.load_config()
        self.assertTrue(cfg["valid"])
        self.assertEqual(cfg["default_sample_rate"], 25.0)

    def test_bundled_file_is_the_fallback(self):
        cfg = lf.load_config()
        self.assertTrue(cfg["valid"])
        self.assertEqual(cfg["default_sample_rate"], 100.0)

    def test_bad_config_keeps_everything(self):
        for broken in ["{not json", json.dumps({"default_sample_rate": 500}),
                       json.dumps({"rules": [{"sample_rate": 10, "conditions": []}]})]:
            os.environ["SAMPLING_CONFIG"] = broken
            cfg = lf.load_config()
            self.assertFalse(cfg["valid"])
            self.assertEqual(cfg["default_sample_rate"], 100.0)
            self.assertEqual(cfg["rules"], [])

    def test_a_bad_config_still_passes_events_through(self):
        os.environ["SAMPLING_CONFIG"] = "{broken"
        lf._CONFIG = None
        out = lf.lambda_handler(firehose(json.dumps(event())))["records"][0]
        self.assertEqual(out["result"], "Ok")



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
        # Same as the SDK: the regex requires scheme and host to match.
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


class TestConfigSources(unittest.TestCase):
    def tearDown(self):
        for key in ("SAMPLING_CONFIG", "SAMPLING_CONFIG_PATH"):
            os.environ.pop(key, None)
        lf._CONFIG = None

    def write(self, payload):
        path = os.path.join(tempfile.mkdtemp(), "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def test_reads_a_file_path(self):
        os.environ["SAMPLING_CONFIG_PATH"] = self.write({"default_sample_rate": 42, "rules": []})
        self.assertEqual(lf.load_config()["default_sample_rate"], 42.0)

    def test_inline_json_beats_a_file_path(self):
        os.environ["SAMPLING_CONFIG_PATH"] = self.write({"default_sample_rate": 42, "rules": []})
        os.environ["SAMPLING_CONFIG"] = json.dumps({"default_sample_rate": 7, "rules": []})
        self.assertEqual(lf.load_config()["default_sample_rate"], 7.0)

    def test_a_blank_env_var_is_ignored(self):
        os.environ["SAMPLING_CONFIG_PATH"] = self.write({"default_sample_rate": 42, "rules": []})
        os.environ["SAMPLING_CONFIG"] = "   "
        self.assertEqual(lf.load_config()["default_sample_rate"], 42.0)

    def test_a_missing_file_fails_open(self):
        os.environ["SAMPLING_CONFIG_PATH"] = "/nonexistent/config.json"
        loaded = lf.load_config()
        self.assertFalse(loaded["valid"])
        self.assertEqual(loaded["default_sample_rate"], 100.0)

    def test_a_non_object_root_fails_open(self):
        os.environ["SAMPLING_CONFIG"] = "[1, 2, 3]"
        self.assertFalse(lf.load_config()["valid"])

    def test_config_is_cached_across_invocations(self):
        os.environ["SAMPLING_CONFIG"] = json.dumps({"default_sample_rate": 100, "rules": []})
        lf._CONFIG = None
        lf.lambda_handler({"records": []})
        first = lf._CONFIG
        lf.lambda_handler({"records": []})
        self.assertIs(lf._CONFIG, first)

    def test_the_shipped_config_file_is_valid(self):
        loaded = lf.load_config()
        self.assertTrue(loaded["valid"], loaded)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
