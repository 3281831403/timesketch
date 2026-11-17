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
"""Shared storage rules for sketch level attributes.

Both the REST API resource (timesketch.api.v1.resources.attribute) and the
analyzer interface (timesketch.lib.analyzers.interface) persist sketch
attributes through the helpers in this module.  This guarantees that the two
write paths share a single identity rule and a single encoding:

* A sketch attribute is identified by its name alone.  A sketch can therefore
  only hold a single attribute row per name.
* The ontology stored on the row follows the most recent write.
* Values are encoded with the ontology that accompanies the write, so a value
  round trips through the database without being mangled.
* Writing the same name/ontology/values twice is idempotent and leaves no extra
  rows behind.
* Overwrites and deletes remove the replaced value rows from the database
  instead of leaving orphaned rows behind.
"""

import logging

from timesketch.lib import ontology as ontology_lib
from timesketch.models import db_session
from timesketch.models.sketch import Attribute
from timesketch.models.sketch import AttributeValue

logger = logging.getLogger("timesketch.sketch_attributes")


def get_cast_as(ontology):
    """Returns the ``cast_as`` type string configured for an ontology.

    Args:
        ontology (str): The ontology name as defined in data/ontology.yaml.

    Returns:
        str: The ontology manager type string used to encode and decode values.

    Raises:
        ValueError: If the ontology is not defined.
    """
    ontology_dict = ontology_lib.ONTOLOGY.get(ontology, {})
    cast_as = ontology_dict.get("cast_as")
    if not cast_as:
        raise ValueError(f"Ontology [{ontology:s}] is not defined.")
    return cast_as


def encode_values(values, ontology):
    """Encodes a list of typed values for storage using the given ontology.

    Args:
        values (list): Values in their native Python type, matching the
            ontology (e.g. a bool for the "bool" ontology, a dict for
            "intelligence").
        ontology (str): The ontology name as defined in data/ontology.yaml.

    Returns:
        list: A list of encoded strings, one per value, ready to be stored.

    Raises:
        ValueError: If the ontology is not defined or a value does not match
            the ontology type.
    """
    cast_as = get_cast_as(ontology)
    encoded_values = []
    for value in values:
        encoded = ontology_lib.OntologyManager.encode_value(value, cast_as)
        if not isinstance(encoded, str):
            raise ValueError(
                "All values need to be stored as strings, got: " f"{type(encoded):s}."
            )
        encoded_values.append(encoded)
    return encoded_values


def set_sketch_attribute(sketch, name, values, ontology="text", user=None):
    """Creates or replaces a sketch attribute using shared storage rules.

    The attribute is identified by its name within the sketch.  If an attribute
    with the same name already exists it is updated in place: its ontology is
    replaced and the old value rows are deleted before the new value rows are
    inserted.  Repeating the exact same write is idempotent, as an unchanged
    attribute (same ontology and encoded values) is left untouched.

    Args:
        sketch (timesketch.models.sketch.Sketch): The sketch to store the
            attribute on.
        name (str): The name of the attribute.
        values (list): The values to store, already encoded as strings by
            :func:`encode_values`.
        ontology (str): The ontology name as defined in data/ontology.yaml.
        user (timesketch.models.user.User): Optional user that owns the write.

    Returns:
        tuple: A two-tuple containing the stored
        :class:`~timesketch.models.sketch.Attribute` object and a boolean that
        is True when a new attribute row was created and False when an
        existing attribute was updated.
    """
    existing_attributes = [
        attribute for attribute in sketch.attributes if attribute.name == name
    ]

    # The name is the identity of an attribute, so there can only be a single
    # row per name.  Older code created a new row when the ontology differed,
    # so remove any duplicate rows and keep the most recently written one.
    attribute = existing_attributes[0] if existing_attributes else None
    for duplicate in existing_attributes[1:]:
        _delete_attribute(duplicate)
    if len(existing_attributes) > 1:
        db_session.flush()

    created = attribute is None
    if created:
        attribute = Attribute(user=user, sketch=sketch, name=name, ontology=ontology)
        db_session.add(attribute)
        db_session.flush()

    existing_values = [attribute_value.value for attribute_value in attribute.values]
    if not created and attribute.ontology == ontology and existing_values == values:
        # Idempotent write: nothing changed, do not touch the stored rows.
        return attribute, False

    attribute.ontology = ontology
    # Hard delete the old value rows so overwrites leave no orphaned values.
    # The delete-orphan cascade on Attribute.values removes them from the
    # database on commit.
    del attribute.values[:]

    for encoded_value in values:
        attribute.values.append(
            AttributeValue(user=user, attribute=attribute, value=encoded_value)
        )

    db_session.add(attribute)
    db_session.commit()
    return attribute, created


def delete_sketch_attribute(sketch, name, ontology=None):
    """Deletes a sketch attribute and all of its value rows.

    Args:
        sketch (timesketch.models.sketch.Sketch): The sketch to delete the
            attribute from.
        name (str): The name of the attribute.
        ontology (str): Optional ontology.  When provided only an attribute
            with a matching ontology is deleted.  The attribute is identified
            by its name, so the ontology is only used to confirm the caller is
            deleting the expected row.

    Returns:
        bool: True if at least one attribute was deleted, False if no matching
        attribute existed.
    """
    matching_attributes = [
        attribute
        for attribute in sketch.attributes
        if attribute.name == name
        and (ontology is None or attribute.ontology == ontology)
    ]

    if not matching_attributes:
        return False

    for attribute in matching_attributes:
        _delete_attribute(attribute)

    db_session.commit()
    return True


def _delete_attribute(attribute):
    """Hard deletes an attribute row together with all of its value rows."""
    # Detaching the value rows from the relationship first marks them for
    # deletion through the delete-orphan cascade; deleting the attribute then
    # removes both rows from the database on commit without leaving orphans.
    del attribute.values[:]
    db_session.delete(attribute)
