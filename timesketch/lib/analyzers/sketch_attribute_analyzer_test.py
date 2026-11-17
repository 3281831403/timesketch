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
"""Tests for the analyzer sketch attribute write path."""

from unittest import mock

from timesketch.api.v1 import utils as api_utils
from timesketch.lib import sketch_attributes
from timesketch.lib.analyzers import interface
from timesketch.lib.testlib import BaseTest
from timesketch.lib.testlib import MockDataStore
from timesketch.models.sketch import Attribute
from timesketch.models.sketch import AttributeValue


class AnalyzerSketchAttributeTest(BaseTest):
    """Tests for BaseSketch.add_sketch_attribute."""

    def setUp(self):
        super().setUp()
        self.sketch = interface.Sketch(sketch_id=1)

    def _get(self, name):
        return api_utils.get_sketch_attributes(self.sketch.sql_sketch)[name]

    def test_text_attribute_round_trip(self):
        """Text values written by an analyzer are encoded like the REST path."""
        self.sketch.add_sketch_attribute("ioc", ["1.2.3.4"], "text")
        self.assertEqual(self._get("ioc")["value"], "1.2.3.4")

    def test_bool_attribute_round_trip(self):
        """Boolean values written by an analyzer survive the round trip."""
        self.sketch.add_sketch_attribute("flag", [True], "bool")
        self.assertIs(self._get("flag")["value"], True)
        self.sketch.add_sketch_attribute("flag", [False], "bool", overwrite=True)
        self.assertIs(self._get("flag")["value"], False)

    def test_dict_attribute_round_trip(self):
        """Dict values written by an analyzer survive the round trip."""
        payload = {"data": [{"ioc": "evil.example"}]}
        self.sketch.add_sketch_attribute(
            "intelligence", [payload], "intelligence", overwrite=True
        )
        self.assertEqual(self._get("intelligence")["value"], payload)

    def test_wrong_type_raises_without_leaving_empty_attribute(self):
        """A value that cannot be encoded raises and leaves no attribute row."""
        with self.assertRaises(ValueError):
            self.sketch.add_sketch_attribute("broken", ["not-a-dict"], "intelligence")
        self.assertEqual(
            Attribute.query.filter_by(
                sketch=self.sketch.sql_sketch, name="broken"
            ).count(),
            0,
        )
        self.assertEqual(AttributeValue.query.count(), 0)

    def test_running_twice_without_overwrite_appends_on_single_row(self):
        """Repeated analyzer runs keep a single attribute row per name."""
        self.sketch.add_sketch_attribute("ioc", ["a"], "text")
        self.sketch.add_sketch_attribute("ioc", ["b"], "text")
        rows = Attribute.query.filter_by(
            sketch=self.sketch.sql_sketch, name="ioc"
        ).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(self._get("ioc")["value"], ["a", "b"])
        self.assertEqual(AttributeValue.query.count(), 2)

    def test_overwrite_replaces_values_without_orphans(self):
        """Overwrite replaces the values and leaves no orphaned rows."""
        self.sketch.add_sketch_attribute("ioc", ["a", "b"], "text")
        self.sketch.add_sketch_attribute("ioc", ["c"], "text", overwrite=True)
        rows = Attribute.query.filter_by(
            sketch=self.sketch.sql_sketch, name="ioc"
        ).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual([value.value for value in rows[0].values], ["c"])
        self.assertEqual(AttributeValue.query.count(), 1)

    def test_ontology_change_keeps_single_row(self):
        """Writing the same name with another ontology adopts the new ontology."""
        self.sketch.add_sketch_attribute("shared", ["hello"], "text")
        self.sketch.add_sketch_attribute("shared", [42], "int", overwrite=True)
        rows = Attribute.query.filter_by(
            sketch=self.sketch.sql_sketch, name="shared"
        ).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].ontology, "int")
        self.assertEqual(self._get("shared")["value"], 42)

    def test_append_keeps_existing_ontology_encoding(self):
        """Appending to an existing attribute keeps its ontology and encoding."""
        self.sketch.add_sketch_attribute("counter", [1], "int")
        # A second run asking for another ontology must not mix encodings.
        self.sketch.add_sketch_attribute("counter", [2], "text")
        attribute = Attribute.query.filter_by(
            sketch=self.sketch.sql_sketch, name="counter"
        ).one()
        self.assertEqual(attribute.ontology, "int")
        self.assertEqual([value.value for value in attribute.values], ["1", "2"])
        self.assertEqual(self._get("counter")["value"], [1, 2])

    def test_intelligence_dict_round_trip_like_yeti(self):
        """The intelligence attribute written as a dict round trips."""
        intelligence = {"data": [{"ioc": "evil.example", "externalURI": "http://x"}]}
        self.sketch.add_sketch_attribute(
            "intelligence", [intelligence], "intelligence", overwrite=True
        )
        self.assertEqual(self._get("intelligence")["value"], intelligence)

    @mock.patch("timesketch.lib.analyzers.interface.OpenSearchDataStore", MockDataStore)
    def test_analyzer_and_rest_paths_share_identity(self):
        """An attribute created by the analyzer is updated through the same rule."""
        self.sketch.add_sketch_attribute("ioc", ["from-analyzer"], "text")
        # The REST storage helper must find the analyzer-created row by name.
        encoded = sketch_attributes.encode_values(["from-rest"], "text")
        sketch_attributes.set_sketch_attribute(
            sketch=self.sketch.sql_sketch,
            name="ioc",
            values=encoded,
            ontology="text",
            user=None,
        )
        rows = Attribute.query.filter_by(
            sketch=self.sketch.sql_sketch, name="ioc"
        ).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(self._get("ioc")["value"], "from-rest")
