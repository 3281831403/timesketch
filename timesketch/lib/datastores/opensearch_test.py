# Copyright 2026 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for OpenSearch datastore."""

# pylint: disable=protected-access

from unittest import mock
from opensearchpy.exceptions import AuthorizationException
from opensearchpy.exceptions import ConnectionTimeout
from opensearchpy.exceptions import TransportError

from timesketch.lib.datastores.opensearch import OpenSearchDataStore
from timesketch.lib.testlib import BaseTest
from timesketch.lib.errors import DatastoreAuthorizationError
from timesketch.lib.errors import DatastoreTimeoutError
from timesketch.lib.errors import UnsupportedDatastoreVersionError


class OpenSearchDataStoreTest(BaseTest):
    """Tests for OpenSearchDataStore."""

    @mock.patch("timesketch.lib.datastores.opensearch.current_user")
    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_search_timeout(self, mock_client, mock_user):
        """Test that search raises DatastoreTimeoutError on ConnectionTimeout."""
        mock_user.username = "testuser"

        # Setup mock client to raise ConnectionTimeout
        mock_es_instance = mock_client.return_value
        mock_es_instance.search.side_effect = ConnectionTimeout(
            "Timeout", "Timeout", "Timeout"
        )

        # Initialize datastore (mocking config is handled by BaseTest/TestConfig)
        ds = OpenSearchDataStore(host="127.0.0.1", port=9200)

        # Ensure the client in the datastore is indeed our mock (it should be)
        ds.client = mock_es_instance
        ds.get_wildcard_fields = mock.Mock(return_value=["message"])

        # Test generic timeout message
        with self.assertRaises(DatastoreTimeoutError) as cm:
            ds.search(sketch_id=1, indices=["test"], query_string="test")

        self.assertIn("The search timed out", str(cm.exception))
        self.assertNotIn("Avoid leading wildcards", str(cm.exception))

        # Test wildcard specific message
        with self.assertRaises(DatastoreTimeoutError) as cm:
            ds.search(sketch_id=1, indices=["test"], query_string="*test")

        self.assertIn("The search timed out", str(cm.exception))
        self.assertIn("Avoid leading wildcards", str(cm.exception))

        # Test wildcard search mode specific message
        with self.assertRaises(DatastoreTimeoutError) as cm:
            ds.search(
                sketch_id=1,
                indices=["test"],
                query_string="*test",
                use_wildcard_fields=True,
            )

        self.assertIn("The search timed out", str(cm.exception))
        self.assertNotIn("Avoid leading wildcards", str(cm.exception))

        # Test timeout message when query_string is None
        with self.assertRaises(DatastoreTimeoutError) as cm:
            ds.search(sketch_id=1, indices=["test"], query_string=None)

        self.assertIn("The search timed out", str(cm.exception))
        self.assertNotIn("Avoid leading wildcards", str(cm.exception))

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_proactive_flush_on_size(self, mock_client):
        """Test that indexing flushes proactively when byte size limit is reached."""
        # Initialize datastore with a very small flush_byte_size
        with mock.patch("timesketch.lib.datastores.opensearch.current_app") as mock_app:
            mock_app.config = {
                "OPENSEARCH_FLUSH_INTERVAL": 1000,
                "OPENSEARCH_FLUSH_BYTE_SIZE": 200,  # Very small limit
            }
            ds = OpenSearchDataStore(host="127.0.0.1", port=9200)

        mock_es_instance = mock_client.return_value
        ds.client = mock_es_instance

        # Mock bulk to return success
        mock_es_instance.bulk.return_value = {"errors": False, "items": []}

        # Add a small event (less than 200 bytes)
        event1 = {"message": "short message"}
        ds.import_event("test_index", event1)
        self.assertEqual(len(ds.import_events), 2)
        mock_es_instance.bulk.assert_not_called()

        # Add another event that pushes it over 200 bytes
        event2 = {"message": "a" * 150}
        ds.import_event("test_index", event2)

        self.assertEqual(mock_es_instance.bulk.call_count, 2)
        self.assertEqual(len(ds.import_events), 0)

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_reactive_halving_on_413(self, mock_client):
        """Test that indexing splits and retries on HTTP 413 error."""
        with mock.patch("timesketch.lib.datastores.opensearch.current_app") as mock_app:
            mock_app.config = {
                "OPENSEARCH_FLUSH_INTERVAL": 1000,
                "OPENSEARCH_FLUSH_BYTE_SIZE": 1024 * 1024,
            }
            ds = OpenSearchDataStore(host="127.0.0.1", port=9200)

        mock_es_instance = mock_client.return_value
        ds.client = mock_es_instance

        # First call to bulk raises 413, subsequent calls succeed
        def bulk_side_effect(body, **_kwargs):
            if len(body) > 4:
                raise TransportError(413, "Request Entity Too Large", "")
            return {"errors": False, "items": []}

        mock_es_instance.bulk.side_effect = bulk_side_effect

        for i in range(4):
            ds.import_event("test_index", {"msg": i})

        results = ds.flush_queued_events()

        # Should have called bulk multiple times due to halving
        # 1. 4 events -> 413
        # 2. 2 events (first half) -> success
        # 3. 2 events (second half) -> success
        self.assertEqual(mock_es_instance.bulk.call_count, 3)
        self.assertFalse(results.get("errors_in_upload"))
        self.assertIn("error_container", results)

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_single_event_too_large(self, mock_client):
        """Test handling of a single event that exceeds the hard OpenSearch limit."""
        with mock.patch("timesketch.lib.datastores.opensearch.current_app") as mock_app:
            mock_app.config = {
                "OPENSEARCH_FLUSH_INTERVAL": 1000,
                "OPENSEARCH_FLUSH_BYTE_SIZE": 1024 * 1024,
            }
            ds = OpenSearchDataStore(host="127.0.0.1", port=9200)

        mock_es_instance = mock_client.return_value
        ds.client = mock_es_instance

        # Always raise 413
        mock_es_instance.bulk.side_effect = TransportError(413, "Too Large", "")

        ds.import_event("test_index", {"msg": "too big"})

        results = ds.flush_queued_events()

        # Should have called bulk once and identified it as single event too large
        self.assertEqual(mock_es_instance.bulk.call_count, 1)
        self.assertTrue(results.get("errors_in_upload"))
        error_container = results.get("error_container")
        self.assertIn("test_index", error_container)
        self.assertEqual(
            error_container["test_index"]["types"]["RequestEntityTooLarge"], 1
        )
        self.assertEqual(
            error_container["test_index"]["details"]["SingleEventTooLarge"], 1
        )

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_flush_queued_events_item_errors(self, mock_client):
        """Test flush_queued_events error handling with and without caused_by."""
        ds = OpenSearchDataStore(host="127.0.0.1", port=9200)
        mock_es_instance = mock_client.return_value
        ds.client = mock_es_instance

        mock_es_instance.bulk.return_value = {
            "errors": True,
            "items": [
                {
                    "index": {
                        "_index": "test_index",
                        "_id": "doc_1",
                        "status": 400,
                        "error": {
                            "type": "mapper_parsing_exception",
                            "reason": "failed to parse field",
                            "caused_by": {
                                "type": "illegal_argument_exception",
                                "reason": "invalid value format provided",
                            },
                        },
                    }
                },
                {
                    "index": {
                        "_index": "test_index",
                        "_id": "doc_2",
                        "status": 400,
                        "error": {
                            "type": "illegal_argument_exception",
                            "reason": (
                                'DocValuesField "original_proto.wildcard" '
                                "is too large, must be <= 32766"
                            ),
                        },
                    }
                },
                {
                    "index": {
                        "_index": "test_index",
                        "_id": "doc_3",
                        "status": 201,
                    }
                },
            ],
        }

        ds.import_event("test_index", {"msg": "event 1"})
        ds.import_event("test_index", {"msg": "event 2"})
        ds.import_event("test_index", {"msg": "event 3"})

        results = ds.flush_queued_events()
        self.assertTrue(results.get("errors_in_upload"))
        error_container = results.get("error_container")
        self.assertIn("test_index", error_container)

        types_counter = error_container["test_index"]["types"]
        details_counter = error_container["test_index"]["details"]
        errors_list = error_container["test_index"]["errors"]

        self.assertEqual(types_counter["mapper_parsing_exception"], 1)
        self.assertEqual(types_counter["illegal_argument_exception"], 1)

        # doc_1 should use caused_by
        self.assertEqual(
            details_counter["illegal_argument_exception/invalid value format provided"],
            1,
        )
        # doc_2 without caused_by should fall back to top-level error type and reason
        self.assertEqual(
            details_counter[
                "illegal_argument_exception/DocValuesField "
                '"original_proto.wildcard" is too large,'
            ],
            1,
        )

        self.assertEqual(len(errors_list), 2)
        self.assertEqual(
            errors_list[0],
            "<mapper_parsing_exception> failed to parse field "
            "[illegal_argument_exception/invalid value format provided]",
        )
        self.assertEqual(
            errors_list[1],
            '<illegal_argument_exception> DocValuesField "original_proto.wildcard" '
            "is too large, must be <= 32766",
        )

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_get_wildcard_fields(self, mock_client):
        """Test get_wildcard_fields mapping parser logic."""
        ds = OpenSearchDataStore(host="127.0.0.1", port=9200)
        mock_es = mock_client.return_value
        ds.client = mock_es

        mock_mappings = {
            "idx_1": {
                "mappings": {
                    "properties": {
                        "msg": {
                            "type": "text",
                            "fields": {
                                "keyword": {"type": "keyword"},
                                "wildcard": {"type": "wildcard"},
                            },
                        },
                        "xml": {"type": "wildcard"},
                        "nested_obj": {
                            "properties": {
                                "sub_field": {
                                    "type": "text",
                                    "fields": {"wildcard": {"type": "wildcard"}},
                                }
                            }
                        },
                        "non_wildcard": {
                            "type": "text",
                            "fields": {"keyword": {"type": "keyword"}},
                        },
                    }
                }
            }
        }
        mock_es.indices.get_mapping.return_value = mock_mappings

        fields = ds.get_wildcard_fields(["idx_1"])
        self.assertCountEqual(fields, ["msg", "xml", "nested_obj.sub_field"])

        # Test with pre-fetched mappings (should not call mock_es.indices.get_mapping)
        mock_es.indices.get_mapping.reset_mock()
        fields_prefetched = ds.get_wildcard_fields(["idx_1"], mappings=mock_mappings)
        self.assertCountEqual(fields_prefetched, ["msg", "xml", "nested_obj.sub_field"])
        mock_es.indices.get_mapping.assert_not_called()

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_build_wildcard_query_dsl_global_search(self, mock_client):
        """Test global wildcard query dsl generation."""
        mock_es = mock_client.return_value
        mock_es.info.return_value = {"version": {"number": "7.0.0"}}
        ds = OpenSearchDataStore(host="127.0.0.1", port=9200)
        wildcard_fields = {"msg", "xml"}

        # 1. Simple global term
        bool_query = ds._build_wildcard_query_dsl("*evil*", wildcard_fields)
        should_clauses = bool_query["should"]
        self.assertEqual(len(should_clauses), 2)
        self.assertEqual(bool_query["minimum_should_match"], 1)

        fields_queried = {
            list(clause["wildcard"].keys())[0] for clause in should_clauses
        }
        self.assertEqual(fields_queried, {"msg.wildcard", "xml.wildcard"})

        for clause in should_clauses:
            field = list(clause["wildcard"].keys())[0]
            self.assertEqual(clause["wildcard"][field]["value"], "*evil*")
            self.assertTrue(clause["wildcard"][field]["case_insensitive"])

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_build_wildcard_query_dsl_field_search(self, mock_client):
        """Test field specific wildcard query dsl generation."""
        mock_es = mock_client.return_value
        mock_es.info.return_value = {"version": {"number": "7.0.0"}}
        ds = OpenSearchDataStore(host="127.0.0.1", port=9200)
        wildcard_fields = {"msg", "xml"}

        # Targeted field mapped to wildcard
        bool_query = ds._build_wildcard_query_dsl("msg:*evil*", wildcard_fields)
        must_clauses = bool_query["must"]
        self.assertEqual(len(must_clauses), 1)
        self.assertEqual(must_clauses[0]["wildcard"]["msg.wildcard"]["value"], "*evil*")
        self.assertTrue(must_clauses[0]["wildcard"]["msg.wildcard"]["case_insensitive"])

        # Targeted field NOT mapped to wildcard -> raises ValueError
        with self.assertRaises(ValueError) as cm:
            ds._build_wildcard_query_dsl("unknown:*evil*", wildcard_fields)

        self.assertIn(
            "Field 'unknown' does not support wildcard search",
            str(cm.exception),
        )

        # Targeted field is '_id' -> exact match (term query)
        bool_query = ds._build_wildcard_query_dsl(
            '_id:"ssj9754BaTwMn7aZPTx2"', wildcard_fields
        )
        must_clauses = bool_query["must"]
        self.assertEqual(len(must_clauses), 1)
        self.assertEqual(must_clauses[0]["term"]["_id"], "ssj9754BaTwMn7aZPTx2")

        # Targeted field is '_id' with wildcard -> still exact match (term query)
        bool_query = ds._build_wildcard_query_dsl("_id:ssj9754*", wildcard_fields)
        must_clauses = bool_query["must"]
        self.assertEqual(len(must_clauses), 1)
        self.assertEqual(must_clauses[0]["term"]["_id"], "ssj9754*")

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_build_wildcard_query_dsl_operators(self, mock_client):
        """Test wildcard query dsl boolean logical operators routing."""
        mock_es = mock_client.return_value
        mock_es.info.return_value = {"version": {"number": "7.0.0"}}
        ds = OpenSearchDataStore(host="127.0.0.1", port=9200)
        wildcard_fields = {"msg", "xml"}

        # Query: msg:*evil* NOT xml:*test* OR *backdoor*
        # Correctly evaluated as: (msg:*evil* AND (NOT xml:*test*)) OR *backdoor*
        query = "msg:*evil* NOT xml:*test* OR *backdoor*"
        bool_query = ds._build_wildcard_query_dsl(query, wildcard_fields)

        # The top-level query must be an OR (should)
        self.assertEqual(len(bool_query["should"]), 3)
        self.assertEqual(bool_query["minimum_should_match"], 1)

        # Left branch of OR: msg:*evil* AND NOT xml:*test*
        left_and = bool_query["should"][0]
        self.assertEqual(len(left_and["bool"]["must"]), 2)

        self.assertEqual(
            left_and["bool"]["must"][0]["wildcard"]["msg.wildcard"]["value"],
            "*evil*",
        )

        # NOT xml:*test*
        not_test = left_and["bool"]["must"][1]
        self.assertEqual(
            not_test["bool"]["must_not"][0]["wildcard"]["xml.wildcard"]["value"],
            "*test*",
        )

        # Right branch of OR: *backdoor* (flattened into the remaining 2 clauses)
        backdoor_clauses = bool_query["should"][1:]
        self.assertEqual(len(backdoor_clauses), 2)

        fields_queried = {
            list(clause["wildcard"].keys())[0] for clause in backdoor_clauses
        }
        self.assertEqual(fields_queried, {"msg.wildcard", "xml.wildcard"})

        for clause in backdoor_clauses:
            field = list(clause["wildcard"].keys())[0]
            self.assertEqual(clause["wildcard"][field]["value"], "*backdoor*")
            self.assertTrue(clause["wildcard"][field]["case_insensitive"])

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_build_wildcard_query_dsl_parentheses(self, mock_client):
        """Test wildcard query dsl parenthetical grouping routing."""
        mock_es = mock_client.return_value
        mock_es.info.return_value = {"version": {"number": "7.0.0"}}
        ds = OpenSearchDataStore(host="127.0.0.1", port=9200)
        wildcard_fields = {"msg", "xml"}

        # Query: msg:*evil* (xml:*test* OR *backdoor*)
        # Evaluated as: msg:*evil* AND (xml:*test* OR *backdoor*)
        query = "msg:*evil* (xml:*test* OR *backdoor*)"
        bool_query = ds._build_wildcard_query_dsl(query, wildcard_fields)

        # Top-level should be a must (AND)
        self.assertEqual(len(bool_query["must"]), 2)

        # Left operand: msg:*evil*
        self.assertEqual(
            bool_query["must"][0]["wildcard"]["msg.wildcard"]["value"],
            "*evil*",
        )

        # Right operand: (xml:*test* OR *backdoor*)
        right_or = bool_query["must"][1]
        self.assertEqual(len(right_or["bool"]["should"]), 3)
        self.assertEqual(right_or["bool"]["minimum_should_match"], 1)
        self.assertEqual(
            right_or["bool"]["should"][0]["wildcard"]["xml.wildcard"]["value"],
            "*test*",
        )

        # The flattened *backdoor* clauses
        backdoor_clauses = right_or["bool"]["should"][1:]
        self.assertEqual(len(backdoor_clauses), 2)

        fields_queried = {
            list(clause["wildcard"].keys())[0] for clause in backdoor_clauses
        }
        self.assertEqual(fields_queried, {"msg.wildcard", "xml.wildcard"})

        for clause in backdoor_clauses:
            field = list(clause["wildcard"].keys())[0]
            self.assertEqual(clause["wildcard"][field]["value"], "*backdoor*")
            self.assertTrue(clause["wildcard"][field]["case_insensitive"])

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_build_wildcard_query_dsl_invalid_field(self, mock_client):
        """Test that querying an unsupported wildcard field raises ValueError."""
        mock_es = mock_client.return_value
        mock_es.info.return_value = {"version": {"number": "7.0.0"}}
        ds = OpenSearchDataStore(host="127.0.0.1", port=9200)
        wildcard_fields = {"msg", "xml"}

        query = "invalid_field:*evil*"
        with self.assertRaises(ValueError) as cm:
            ds._build_wildcard_query_dsl(query, wildcard_fields)

        self.assertIn(
            "Field 'invalid_field' does not support wildcard search",
            str(cm.exception),
        )

    @mock.patch("timesketch.lib.datastores.opensearch.current_app")
    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_unsupported_version(self, mock_client, mock_app):
        """Test that initializing OpenSearchDataStore raises error on
        unsupported version."""
        mock_app.config = {"TESTING": False}
        mock_es_instance = mock_client.return_value
        mock_es_instance.info.return_value = {"version": {"number": "2.15.0"}}

        with self.assertRaises(UnsupportedDatastoreVersionError) as cm:

            OpenSearchDataStore(host="127.0.0.1", port=9200)

        self.assertIn(
            "Connected OpenSearch version 2.15.0 is not supported", str(cm.exception)
        )

    @mock.patch("timesketch.lib.datastores.opensearch.current_app")
    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_supported_version(self, mock_client, mock_app):
        """Test that initializing OpenSearchDataStore succeeds on supported version."""
        mock_app.config = {"TESTING": False}
        mock_es_instance = mock_client.return_value
        mock_es_instance.info.return_value = {"version": {"number": "2.19.5"}}

        ds = OpenSearchDataStore(host="127.0.0.1", port=9200)
        self.assertEqual(ds.version, "2.19.5")

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_create_index_authorization_error_on_exists(self, mock_client):
        """Test that a 403 when checking the index raises a clear error."""
        mock_es_instance = mock_client.return_value
        mock_es_instance.indices.exists.side_effect = AuthorizationException(
            403, "security_exception", {}
        )
        ds = OpenSearchDataStore(host="127.0.0.1", port=9200)

        with self.assertLogs("timesketch.opensearch", level="ERROR"):
            with self.assertRaises(DatastoreAuthorizationError) as cm:
                ds.create_index(index_name="legacyindex")

        self.assertIn("[legacyindex]", str(cm.exception))
        self.assertIn("HTTP 403", str(cm.exception))
        self.assertNotIn("OPENSEARCH_INDEX_PREFIX", str(cm.exception))
        mock_es_instance.indices.create.assert_not_called()

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_create_index_authorization_error_on_create(self, mock_client):
        """Test that a 403 when creating the index mentions the prefix."""
        self.app.config["OPENSEARCH_INDEX_PREFIX"] = "timesketch-"
        mock_es_instance = mock_client.return_value
        mock_es_instance.indices.exists.return_value = False
        mock_es_instance.indices.create.side_effect = AuthorizationException(
            403, "security_exception", {}
        )
        ds = OpenSearchDataStore(host="127.0.0.1", port=9200)

        with self.assertLogs("timesketch.opensearch", level="ERROR"):
            with self.assertRaises(DatastoreAuthorizationError) as cm:
                ds.create_index(index_name="otherindex")

        self.assertIn("[otherindex]", str(cm.exception))
        self.assertIn("OPENSEARCH_INDEX_PREFIX 'timesketch-'", str(cm.exception))

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_create_index_existing_index(self, mock_client):
        """Test that existing indices are not created again."""
        mock_es_instance = mock_client.return_value
        mock_es_instance.indices.exists.return_value = True
        ds = OpenSearchDataStore(host="127.0.0.1", port=9200)

        self.assertEqual(ds.create_index(index_name="existing"), "existing")
        mock_es_instance.indices.create.assert_not_called()

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_create_index_without_name(self, mock_client):
        """Test that a new prefixed index name is generated for every call."""
        self.app.config["OPENSEARCH_INDEX_PREFIX"] = "timesketch-"
        mock_es_instance = mock_client.return_value
        mock_es_instance.indices.exists.return_value = False
        ds = OpenSearchDataStore(host="127.0.0.1", port=9200)

        first_index_name = ds.create_index()
        second_index_name = ds.create_index()

        self.assertRegex(first_index_name, r"^timesketch-[0-9a-f]{32}$")
        self.assertRegex(second_index_name, r"^timesketch-[0-9a-f]{32}$")
        self.assertNotEqual(first_index_name, second_index_name)
        mock_es_instance.indices.create.assert_any_call(
            index=first_index_name, body=mock.ANY
        )


class OpenSearchBuildQueryTest(BaseTest):
    """Tests for build_query filter chip semantics.

    These tests assert the actual query body that is sent to OpenSearch
    for fixed chip combinations, making sure search, count, CSV export
    and Explore aggregations all share the same filter semantics.
    """

    # Fixed chip combinations used across the tests.
    TERM_CHIP = {
        "type": "term",
        "field": "hostname",
        "value": "evil.example.com",
        "operator": "must",
        "active": True,
    }
    TERM_CHIP_EXCLUDE = {
        "type": "term",
        "field": "username",
        "value": "system",
        "operator": "must_not",
        "active": True,
    }
    LABEL_CHIP = {
        "type": "label",
        "field": "",
        "value": "bad",
        "operator": "must",
        "active": True,
    }
    DATETIME_CHIP_1 = {
        "type": "datetime_range",
        "field": "",
        "value": "2023-01-01T00:00:00,2023-01-02T00:00:00",
        "operator": "must",
        "active": True,
    }
    DATETIME_CHIP_2 = {
        "type": "datetime_range",
        "field": "",
        "value": "2023-02-01T00:00:00,2023-02-02T00:00:00",
        "operator": "must",
        "active": True,
    }

    EXPECTED_TERM_FILTER = {
        "match_phrase": {"hostname.keyword": {"query": "evil.example.com"}}
    }
    EXPECTED_TERM_FILTER_EXCLUDE = {
        "match_phrase": {"username.keyword": {"query": "system"}}
    }
    EXPECTED_LABEL_FILTER = {
        "bool": {
            "must": [
                {
                    "nested": {
                        "query": {
                            "bool": {
                                "must": [
                                    {"term": {"timesketch_label.name.keyword": "bad"}},
                                    {"term": {"timesketch_label.sketch_id": 1}},
                                ]
                            }
                        },
                        "path": "timesketch_label",
                    }
                }
            ]
        }
    }
    EXPECTED_STAR_FILTER = {
        "bool": {
            "must": [
                {
                    "nested": {
                        "query": {
                            "bool": {
                                "must": [
                                    {
                                        "term": {
                                            "timesketch_label.name.keyword": "__ts_star"
                                        }
                                    },
                                    {"term": {"timesketch_label.sketch_id": 1}},
                                ]
                            }
                        },
                        "path": "timesketch_label",
                    }
                }
            ]
        }
    }
    EXPECTED_DATETIME_UNION = {
        "bool": {
            "should": [
                {
                    "range": {
                        "datetime": {
                            "gte": "2023-01-01T00:00:00",
                            "lte": "2023-01-02T00:00:00",
                        }
                    }
                },
                {
                    "range": {
                        "datetime": {
                            "gte": "2023-02-01T00:00:00",
                            "lte": "2023-02-02T00:00:00",
                        }
                    }
                },
            ],
            "minimum_should_match": 1,
        }
    }
    EXPECTED_QUERY_STRING = {"query_string": {"query": "*", "default_operator": "AND"}}

    def _get_datastore(self, mock_client):
        """Create a datastore instance backed by a mocked client."""
        mock_es = mock_client.return_value
        mock_es.info.return_value = {"version": {"number": "2.19.5"}}
        datastore = OpenSearchDataStore(host="127.0.0.1", port=9200)
        datastore.client = mock_es
        return datastore

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_term_chips_filter_exactly_once(self, mock_client):
        """Active term chips (incl. exclusion) filter exactly once."""
        datastore = self._get_datastore(mock_client)
        query_filter = {"chips": [self.TERM_CHIP, self.TERM_CHIP_EXCLUDE]}

        query = datastore.build_query(1, "*", query_filter)

        bool_query = query["query"]["bool"]
        self.assertEqual(
            bool_query["must"], [self.EXPECTED_QUERY_STRING, self.EXPECTED_TERM_FILTER]
        )
        self.assertEqual(bool_query["must_not"], [self.EXPECTED_TERM_FILTER_EXCLUDE])
        self.assertEqual(bool_query["filter"], [])
        # No empty label/datetime bool clauses may be emitted.
        self.assertNotIn({"bool": {"must": []}}, bool_query["must"])
        self.assertNotIn(
            {"bool": {"should": [], "minimum_should_match": 1}}, bool_query["must"]
        )

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_label_chip_builds_single_nested_filter(self, mock_client):
        """A label chip produces exactly one nested label filter."""
        datastore = self._get_datastore(mock_client)
        query_filter = {"chips": [self.LABEL_CHIP]}

        query = datastore.build_query(1, "*", query_filter)

        bool_query = query["query"]["bool"]
        self.assertEqual(
            bool_query["must"], [self.EXPECTED_QUERY_STRING, self.EXPECTED_LABEL_FILTER]
        )
        self.assertEqual(bool_query["must_not"], [])

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_datetime_chips_are_unioned(self, mock_client):
        """Multiple datetime chips are combined with OR (should)."""
        datastore = self._get_datastore(mock_client)
        query_filter = {"chips": [self.DATETIME_CHIP_1, self.DATETIME_CHIP_2]}

        query = datastore.build_query(1, "*", query_filter)

        bool_query = query["query"]["bool"]
        self.assertEqual(
            bool_query["must"],
            [self.EXPECTED_QUERY_STRING, self.EXPECTED_DATETIME_UNION],
        )

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_mixed_chips_do_not_duplicate_filters(self, mock_client):
        """Label and datetime filters are appended once, not per chip."""
        datastore = self._get_datastore(mock_client)
        query_filter = {
            "chips": [self.LABEL_CHIP, self.DATETIME_CHIP_1, self.TERM_CHIP]
        }

        query = datastore.build_query(1, "*", query_filter)

        bool_query = query["query"]["bool"]
        expected_datetime = {
            "bool": {
                "should": [self.EXPECTED_DATETIME_UNION["bool"]["should"][0]],
                "minimum_should_match": 1,
            }
        }
        self.assertEqual(
            bool_query["must"],
            [
                self.EXPECTED_QUERY_STRING,
                self.EXPECTED_TERM_FILTER,
                self.EXPECTED_LABEL_FILTER,
                expected_datetime,
            ],
        )
        # Each filter clause appears exactly once.
        self.assertEqual(bool_query["must"].count(self.EXPECTED_LABEL_FILTER), 1)
        self.assertEqual(bool_query["must"].count(expected_datetime), 1)

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_inactive_chips_are_skipped(self, mock_client):
        """Chips the user disabled do not contribute any filter clause."""
        datastore = self._get_datastore(mock_client)
        inactive_term = dict(self.TERM_CHIP, active=False)
        inactive_label = dict(self.LABEL_CHIP, active=False)
        query_filter = {"chips": [inactive_term, inactive_label]}

        query = datastore.build_query(1, "*", query_filter)

        bool_query = query["query"]["bool"]
        self.assertEqual(bool_query["must"], [self.EXPECTED_QUERY_STRING])
        self.assertEqual(bool_query["must_not"], [])

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_star_filter_keeps_only_starred_events(self, mock_client):
        """The legacy star filter flag maps to the __ts_star label."""
        datastore = self._get_datastore(mock_client)
        query_filter = {"star": True}

        query = datastore.build_query(1, "*", query_filter)

        bool_query = query["query"]["bool"]
        self.assertEqual(
            bool_query["must"], [self.EXPECTED_QUERY_STRING, self.EXPECTED_STAR_FILTER]
        )

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_star_filter_combined_with_chips(self, mock_client):
        """Star flag and chips stack within the same filter semantics."""
        datastore = self._get_datastore(mock_client)
        query_filter = {"star": True, "chips": [self.TERM_CHIP]}

        query = datastore.build_query(1, "*", query_filter)

        bool_query = query["query"]["bool"]
        self.assertEqual(
            bool_query["must"],
            [
                self.EXPECTED_QUERY_STRING,
                self.EXPECTED_TERM_FILTER,
                self.EXPECTED_STAR_FILTER,
            ],
        )

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_dsl_and_chips_are_both_applied(self, mock_client):
        """A saved-view DSL stacked with chips keeps both filters."""
        datastore = self._get_datastore(mock_client)
        query_dsl = {"query": {"match_all": {}}}
        query_filter = {
            "chips": [self.TERM_CHIP, self.TERM_CHIP_EXCLUDE],
            "from": 40,
            "size": 40,
            "order": "desc",
        }

        query = datastore.build_query(1, "", query_filter, query_dsl=query_dsl)

        self.assertEqual(
            query["query"],
            {
                "bool": {
                    "must": [{"match_all": {}}, self.EXPECTED_TERM_FILTER],
                    "must_not": [self.EXPECTED_TERM_FILTER_EXCLUDE],
                    "filter": [],
                }
            },
        )
        # Pagination and sorting are preserved.
        self.assertEqual(query["from"], 40)
        self.assertEqual(query["size"], 40)
        self.assertEqual(query["sort"], {"datetime": "desc"})

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_dsl_without_chips_is_untouched(self, mock_client):
        """A DSL without chips keeps its original query structure."""
        datastore = self._get_datastore(mock_client)
        query_dsl = {"query": {"match_all": {}}}

        query = datastore.build_query(1, "", {}, query_dsl=query_dsl)

        self.assertEqual(query["query"], {"match_all": {}})
        self.assertEqual(query["sort"], {"datetime": "asc"})

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_pagination_and_sort_defaults(self, mock_client):
        """Return structure, pagination and sorting stay unchanged."""
        datastore = self._get_datastore(mock_client)
        query_filter = {"chips": [self.TERM_CHIP], "from": 80, "size": 40}

        query = datastore.build_query(1, "*", query_filter)

        self.assertEqual(query["from"], 80)
        self.assertEqual(query["size"], 40)
        self.assertEqual(query["sort"], {"datetime": "asc"})
        self.assertEqual(
            set(query["query"]["bool"].keys()), {"must", "must_not", "filter"}
        )

    @mock.patch("timesketch.lib.datastores.opensearch.OpenSearch")
    def test_search_and_count_share_filter_semantics(self, mock_client):
        """The query body sent for search and count uses the same filters."""
        datastore = self._get_datastore(mock_client)
        mock_es = mock_client.return_value
        mock_es.search.return_value = {"hits": {"hits": [], "total": 0}, "took": 0}
        mock_es.count.return_value = {"count": 0}

        query_filter = {"star": True, "chips": [self.TERM_CHIP]}
        expected_must = [
            self.EXPECTED_QUERY_STRING,
            self.EXPECTED_TERM_FILTER,
            self.EXPECTED_STAR_FILTER,
        ]

        datastore.search(
            sketch_id=1, indices=["test"], query_string="*", query_filter=query_filter
        )
        search_body = mock_es.search.call_args[1]["body"]
        self.assertEqual(search_body["query"]["bool"]["must"], expected_must)

        datastore.search(
            sketch_id=1,
            indices=["test"],
            query_string="*",
            query_filter=query_filter,
            count=True,
        )
        count_body = mock_es.count.call_args[1]["body"]
        self.assertEqual(count_body["query"]["bool"]["must"], expected_must)
