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
"""Tests for the shared sketch attribute storage rules."""

from timesketch.api.v1 import utils as api_utils
from timesketch.lib import sketch_attributes
from timesketch.lib.testlib import ModelBaseTest
from timesketch.models.sketch import Attribute
from timesketch.models.sketch import AttributeValue


class SketchAttributeStorageTest(ModelBaseTest):
    """Tests for timesketch.lib.sketch_attributes."""

    def _set(self, name, values, ontology="text"):
        encoded = sketch_attributes.encode_values(values, ontology)
        sketch_attributes.set_sketch_attribute(
            sketch=self.sketch1,
            name=name,
            values=encoded,
            ontology=ontology,
            user=self.user1,
        )

    def _attributes(self):
        return api_utils.get_sketch_attributes(self.sketch1)

    def _row_count(self, name):
        return (
            Attribute.query.filter_by(sketch=self.sketch1, name=name).count(),
            AttributeValue.query.join(Attribute)
            .filter(Attribute.sketch == self.sketch1, Attribute.name == name)
            .count(),
        )

    def test_text_round_trip_and_scalar_shape(self):
        """A single text value round trips and is returned as a scalar."""
        self._set("ioc", ["1.2.3.4"], "text")
        self.assertEqual(self._attributes()["ioc"]["value"], "1.2.3.4")
        self.assertEqual(self._attributes()["ioc"]["ontology"], "text")

    def test_multiple_values_returned_as_list(self):
        """Multiple values are returned as a list."""
        self._set("ioc", ["a", "b"], "text")
        self.assertEqual(self._attributes()["ioc"]["value"], ["a", "b"])

    def test_repeated_write_is_idempotent(self):
        """Writing the exact same attribute twice leaves a single row."""
        self._set("ioc", ["1.2.3.4"], "text")
        self._set("ioc", ["1.2.3.4"], "text")
        self.assertEqual(self._row_count("ioc"), (1, 1))

    def test_overwrite_removes_old_value_rows(self):
        """Overwriting an attribute leaves no orphaned value rows behind."""
        self._set("ioc", ["a", "b"], "text")
        self._set("ioc", ["c"], "text")
        self.assertEqual(self._row_count("ioc"), (1, 1))
        self.assertEqual(self._attributes()["ioc"]["value"], "c")

    def test_bool_round_trip(self):
        """Boolean values survive a storage round trip."""
        self._set("flag", [True], "bool")
        self.assertIs(self._attributes()["flag"]["value"], True)
        self._set("flag", [False], "bool")
        self.assertIs(self._attributes()["flag"]["value"], False)

    def test_dict_round_trip(self):
        """Dict values survive a storage round trip."""
        payload = {"k": 1, "nested": {"x": ["y"]}}
        self._set("intel", [payload], "intelligence")
        self.assertEqual(self._attributes()["intel"]["value"], payload)

    def test_dict_ontology_rejects_non_dict(self):
        """A non-dict value for a dict ontology raises without creating rows."""
        with self.assertRaises(ValueError):
            sketch_attributes.encode_values(["not-a-dict"], "intelligence")
        self.assertEqual(Attribute.query.filter_by(sketch=self.sketch1).count(), 0)
        self.assertEqual(AttributeValue.query.count(), 0)

    def test_int_and_float_round_trip(self):
        """Integer and float values keep their type through a round trip."""
        self._set("score", [7], "int")
        self._set("ratio", [1.5], "float")
        score = self._attributes()["score"]["value"]
        ratio = self._attributes()["ratio"]["value"]
        self.assertEqual(score, 7)
        self.assertIsInstance(score, int)
        self.assertEqual(ratio, 1.5)
        self.assertIsInstance(ratio, float)

    def test_unknown_ontology_rejected(self):
        """Values for an unknown ontology cannot be encoded."""
        with self.assertRaises(ValueError):
            sketch_attributes.encode_values(["x"], "no-such-ontology")

    def test_ontology_follows_latest_write(self):
        """Rewriting with another ontology keeps one row and adopts the ontology."""
        self._set("shared", ["hello"], "text")
        self._set("shared", [42], "int")
        rows = Attribute.query.filter_by(sketch=self.sketch1, name="shared").all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].ontology, "int")
        self.assertEqual(self._attributes()["shared"]["value"], 42)
        self.assertEqual(self._row_count("shared"), (1, 1))

    def test_delete_removes_attribute_and_value_rows(self):
        """Deleting an attribute hard deletes the attribute and value rows."""
        self._set("ioc", ["a", "b"], "text")
        deleted = sketch_attributes.delete_sketch_attribute(self.sketch1, "ioc", "text")
        self.assertTrue(deleted)
        self.assertEqual(self._row_count("ioc"), (0, 0))
        self.assertNotIn("ioc", self._attributes())

    def test_delete_with_wrong_ontology_is_a_noop(self):
        """The ontology argument must match before an attribute is deleted."""
        self._set("ioc", ["a"], "text")
        deleted = sketch_attributes.delete_sketch_attribute(self.sketch1, "ioc", "int")
        self.assertFalse(deleted)
        self.assertEqual(self._row_count("ioc"), (1, 1))

    def test_delete_missing_attribute_returns_false(self):
        """Deleting a non existing attribute returns False."""
        self.assertFalse(
            sketch_attributes.delete_sketch_attribute(self.sketch1, "missing")
        )

    def test_legacy_duplicate_rows_are_healed(self):
        """Pre-existing duplicate rows for a name collapse into a single row."""
        legacy = Attribute(
            user=self.user1, sketch=self.sketch1, name="dup", ontology="text"
        )
        self._commit_to_database(legacy)
        legacy.values.append(
            AttributeValue(user=self.user1, attribute=legacy, value="old")
        )
        self._commit_to_database(legacy)
        legacy2 = Attribute(
            user=self.user1, sketch=self.sketch1, name="dup", ontology="int"
        )
        self._commit_to_database(legacy2)
        legacy2.values.append(
            AttributeValue(user=self.user1, attribute=legacy2, value="99")
        )
        self._commit_to_database(legacy2)

        self._set("dup", ["new"], "text")

        rows = Attribute.query.filter_by(sketch=self.sketch1, name="dup").all()
        self.assertEqual(len(rows), 1)
        self.assertEqual([value.value for value in rows[0].values], ["new"])
        self.assertEqual(
            AttributeValue.query.filter_by(attribute_id=legacy2.id).count(), 0
        )
