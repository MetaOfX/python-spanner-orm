# python3
# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Holds table-specific information to make querying spanner eaiser."""

from __future__ import annotations

import collections
import copy
from typing import Any, Callable, Dict, Iterable, List, Optional, Type, TypeVar, Union

from spanner_orm import api
from spanner_orm import condition
from spanner_orm import error
from spanner_orm import field
from spanner_orm import index
from spanner_orm import metadata
from spanner_orm import query
from spanner_orm import registry
from spanner_orm import relationship

from google.cloud import spanner
from google.cloud.spanner_v1 import transaction as spanner_transaction


class ModelMetaclass(type):
  """Populates ModelMetadata based on class attributes."""

  def __new__(mcs, name: str, bases: Any, attrs: Dict[str, Any], **kwargs: Any):
    parents = [base for base in bases if isinstance(base, ModelMetaclass)]
    if not parents:
      return super().__new__(mcs, name, bases, attrs, **kwargs)

    model_metadata = metadata.ModelMetadata()
    for parent in parents:
      if 'meta' in vars(parent):
        model_metadata.add_metadata(parent.meta)

    non_model_attrs = {}
    for key, value in attrs.items():
      if key == '__table__':
        model_metadata.table = value
      elif key == '__interleaved__':
        model_metadata.interleaved = value
      if isinstance(value, field.Field):
        model_metadata.add_field(key, value)
      elif isinstance(value, index.Index):
        model_metadata.add_index(key, value)
      elif isinstance(value, relationship.Relationship):
        model_metadata.add_relation(key, value)
      else:
        non_model_attrs[key] = value

    cls = super().__new__(mcs, name, bases, non_model_attrs, **kwargs)

    # If a table is set, this class represents a complete model, so finalize
    # the metadata
    if model_metadata.table:
      model_metadata.model_class = cls
      model_metadata.finalize()
    cls.meta = model_metadata
    return cls

  def __getattr__(
      cls,
      name: str) -> Union[field.Field, relationship.Relationship, index.Index]:
    # Unclear why pylint doesn't like this
    # pylint: disable=unsupported-membership-test
    if name in cls.schema:
      return cls.schema[name]
    elif name in cls.relations:
      return cls.relations[name]
    elif name in cls.indexes:
      return cls.indexes[name]
    # pylint: enable=unsupported-membership-test
    raise AttributeError(name)

  @property
  def column_prefix(cls) -> str:
    return cls.table.split('.')[-1]

  # Table schema class methods
  @property
  def columns(cls) -> List[str]:
    return cls.meta.columns

  @property
  def indexes(cls) -> Dict[str, index.Index]:
    return cls.meta.indexes

  @property
  def interleaved(cls) -> Optional[Type[Model]]:
    if cls.meta.interleaved:
      return registry.model_registry().get(cls.meta.interleaved)
    return None

  @property
  def primary_keys(cls) -> List[str]:
    return cls.meta.primary_keys

  @property
  def relations(cls) -> Dict[str, relationship.Relationship]:
    return cls.meta.relations

  @property
  def schema(cls) -> Dict[str, field.Field]:
    return cls.meta.fields

  @property
  def table(cls):
    return cls.meta.table

  def validate_value(cls, field_name, value, error_type=error.SpannerError):
    try:
      cls.schema[field_name].validate(value)
    except error.ValidationError as ex:
      raise error_type(*ex.args)


CallableReturn = TypeVar('CallableReturn')


class ModelApi(metaclass=ModelMetaclass):
  """Implements class-level Spanner queries on top of ModelMetaclass."""

  @classmethod
  def spanner_api(cls) -> api.SpannerApi:
    return api.spanner_api()

  # Table read methods
  @classmethod
  def all(cls, transaction: Optional[spanner_transaction.Transaction] = None
         ) -> List[ModelObject]:
    args = [cls.table, cls.columns, spanner.KeySet(all_=True)]
    results = cls._execute_read(cls.spanner_api().find, transaction, args)
    return cls._results_to_models(results)

  @classmethod
  def count(cls, transaction: Optional[spanner_transaction.Transaction],
            *conditions: condition.Condition) -> int:
    """Implementation of the SELECT COUNT query."""
    builder = query.CountQuery(cls, conditions)
    args = [builder.sql(), builder.parameters(), builder.types()]
    results = cls._execute_read(cls.spanner_api().sql_query, transaction, args)
    return builder.process_results(results)

  @classmethod
  def count_equal(cls,
                  transaction: Optional[spanner_transaction.Transaction] = None,
                  **constraints: Any) -> int:
    """Creates and executes a SELECT COUNT query from constraints."""
    conditions = []
    for column, value in constraints.items():
      if isinstance(value, list):
        conditions.append(condition.in_list(column, value))
      else:
        conditions.append(condition.equal_to(column, value))
    return cls.count(transaction, *conditions)

  @classmethod
  def find(cls,
           transaction: Optional[spanner_transaction.Transaction] = None,
           **keys: Any) -> Optional[ModelObject]:
    """Executes a FIND for a single item, based on the provided key."""
    resources = cls.find_multi(transaction, [keys])
    return resources[0] if resources else None

  @classmethod
  def find_multi(cls, transaction: Optional[spanner_transaction.Transaction],
                 keys: Iterable[Dict[str, Any]]) -> List[ModelObject]:
    """Executes a FIND for multiple items, based on the provided keys."""
    key_values = []
    for key in keys:
      key_values.append([key[column] for column in cls.primary_keys])
    keyset = spanner.KeySet(keys=key_values)

    args = [cls.table, cls.columns, keyset]
    results = cls._execute_read(cls.spanner_api().find, transaction, args)
    return cls._results_to_models(results)

  @classmethod
  def where(cls, transaction: Optional[spanner_transaction.Transaction],
            *conditions: condition.Condition) -> List[ModelObject]:
    """Implementation of the SELECT query."""
    builder = query.SelectQuery(cls, conditions)
    args = [builder.sql(), builder.parameters(), builder.types()]
    results = cls._execute_read(cls.spanner_api().sql_query, transaction, args)
    return builder.process_results(results)

  @classmethod
  def where_equal(cls,
                  transaction: Optional[spanner_transaction.Transaction] = None,
                  **constraints: Any) -> List[ModelObject]:
    """Creates and executes a SELECT query from constraints."""
    conditions = []
    for column, value in constraints.items():
      if isinstance(value, list):
        conditions.append(condition.in_list(column, value))
      else:
        conditions.append(condition.equal_to(column, value))
    return cls.where(transaction, *conditions)

  @classmethod
  def _results_to_models(cls,
                         results: Iterable[Iterable[Any]]) -> List[ModelObject]:
    items = [dict(zip(cls.columns, result)) for result in results]
    return [cls(item, persisted=True) for item in items]

  @classmethod
  def _execute_read(cls, db_api: Callable[..., CallableReturn],
                    transaction: Optional[spanner_transaction.Transaction],
                    args: List[Any]) -> CallableReturn:
    if transaction is not None:
      return db_api(transaction, *args)
    else:
      return cls.spanner_api().run_read_only(db_api, *args)

  # Table write methods
  @classmethod
  def create(cls,
             transaction: Optional[spanner_transaction.Transaction] = None,
             **kwargs: Any) -> None:
    cls._execute_write(cls.spanner_api().insert, transaction, [kwargs])

  @classmethod
  def create_or_update(
      cls,
      transaction: Optional[spanner_transaction.Transaction] = None,
      **kwargs: Any):
    cls._execute_write(cls.spanner_api().upsert, transaction, [kwargs])

  @classmethod
  def delete_batch(cls, transaction: Optional[spanner_transaction.Transaction],
                   models: List[ModelObject]) -> None:
    """Delete from Spanner all rows corresponding to provided models."""
    key_list = []
    for model in models:
      key_list.append([getattr(model, column) for column in cls.primary_keys])
    keyset = spanner.KeySet(keys=key_list)

    db_api = cls.spanner_api().delete
    args = [cls.table, keyset]
    if transaction is not None:
      return db_api(transaction, *args)
    else:
      return cls.spanner_api().run_write(db_api, *args)

  @classmethod
  def save_batch(cls,
                 transaction: Optional[spanner_transaction.Transaction],
                 models: List[ModelObject],
                 force_write: bool = False) -> None:
    """Persist all model changes in list of models to Spanner.

    Args:
      transaction: existing transaction to use. If None, a new transaction is
        used automatically. In this case, multiple transactions may be created
      models: list of models to persist to Spanner
      force_write: If true, we use the Spanner upsert API so no exceptions are
        thrown. If false, we use insert/update according to the _persisted flag
        so that an exception is thrown if that flag does not match the actual
        state of the object.
    """
    work = collections.defaultdict(list)
    for model in models:
      value = {column: getattr(model, column) for column in cls.columns}
      if force_write:
        api_method = cls.spanner_api().upsert
      elif model._persisted:  # pylint: disable=protected-access
        api_method = cls.spanner_api().update
      else:
        api_method = cls.spanner_api().insert
      work[api_method].append(value)
      model._persisted = True  # pylint: disable=protected-access
    for api_method, values in work.items():
      cls._execute_write(api_method, transaction, values)

  @classmethod
  def update(cls,
             transaction: Optional[spanner_transaction.Transaction] = None,
             **kwargs: Any) -> None:
    cls._execute_write(cls.spanner_api().update, transaction, [kwargs])

  @classmethod
  def _execute_write(cls, db_api: Callable[..., Any],
                     transaction: Optional[spanner_transaction.Transaction],
                     dictionaries: Iterable[Dict[str, Any]]) -> None:
    """Validates all write value types and commits write to Spanner."""
    columns, values = None, []
    for dictionary in dictionaries:
      invalid_keys = set(dictionary.keys()) - set(cls.columns)
      if invalid_keys:
        raise error.SpannerError('Invalid keys set on {model}: {keys}'.format(
            model=cls.__name__, keys=invalid_keys))

      if columns is None:
        columns = dictionary.keys()
      if columns != dictionary.keys():
        raise error.SpannerError(
            'Attempted to update rows with different sets of keys')

      for key, value in dictionary.items():
        cls.validate_value(key, value, error.SpannerError)
      values.append([dictionary[column] for column in columns])

    args = [cls.table, columns, values]
    if transaction is not None:
      return db_api(transaction, *args)
    else:
      return cls.spanner_api().run_write(db_api, *args)


class Model(ModelApi):
  """Maps to a table in spanner and has basic functions for querying tables."""

  def __init__(self, values: Dict[str, Any], persisted: bool = False):
    start_values = {}
    self.__dict__['start_values'] = start_values
    self.__dict__['_persisted'] = persisted

    # If the values came from Spanner, trust them and skip validation
    if not persisted:
      # An object is invalid if primary key values are missing
      missing_keys = set(self._primary_keys) - set(values.keys())
      if missing_keys:
        raise error.SpannerError(
            'All primary keys must be specified. Missing: {keys}'.format(
                keys=missing_keys))

      for column in self._columns:
        self._metaclass.validate_value(column, values.get(column), ValueError)

    for column in self._columns:
      value = values.get(column)
      start_values[column] = copy.copy(value)
      self.__dict__[column] = value

    for relation in self._relations:
      if relation in values:
        self.__dict__[relation] = values[relation]

  def __setattr__(self, name: str, value: Any) -> None:
    if name in self._relations:
      raise AttributeError(name)
    elif name in self._fields:
      if name in self._primary_keys:
        raise AttributeError(name)
      self._metaclass.validate_value(name, value, AttributeError)
    super().__setattr__(name, value)

  @property
  def _metaclass(self) -> Type[Model]:
    return type(self)

  @property
  def _columns(self) -> List[str]:
    return self._metaclass.columns

  @property
  def _fields(self) -> Dict[str, field.Field]:
    return self._metaclass.schema

  @property
  def _primary_keys(self) -> List[str]:
    return self._metaclass.primary_keys

  @property
  def _relations(self) -> Dict[str, relationship.Relationship]:
    return self._metaclass.relations

  @property
  def _table(self) -> str:
    return self._metaclass.table

  @property
  def values(self) -> Dict[str, Any]:
    return {key: getattr(self, key) for key in self._columns}

  def changes(self) -> Dict[str, Any]:
    values = self.values
    return {
        key: values[key]
        for key in self._columns
        if values[key] != self.start_values.get(key)
    }

  def delete(self, transaction: spanner_transaction.Transaction = None) -> None:
    key = [getattr(self, column) for column in self._primary_keys]
    keyset = spanner.KeySet([key])

    db_api = self.spanner_api().delete
    args = [self._table, keyset]
    if transaction is not None:
      db_api(transaction, *args)
    else:
      self.spanner_api().run_write(db_api, *args)

  def id(self) -> Dict[str, Any]:
    return {key: self.values[key] for key in self._primary_keys}

  def reload(self, transaction: spanner_transaction.Transaction = None
            ) -> Optional[Model]:
    updated_object = self._metaclass.find(transaction, **self.id())
    if updated_object is None:
      return None
    for column in self._columns:
      if column not in self._primary_keys:
        setattr(self, column, getattr(updated_object, column))
    self._persisted = True
    return self

  def save(self, transaction: spanner_transaction.Transaction = None) -> Model:
    if self._persisted:
      changed_values = self.changes()
      if changed_values:
        changed_values.update(self.id())
        self._metaclass.update(transaction, **changed_values)
    else:
      self._metaclass.create(transaction, **self.values)
      self._persisted = True
    return self


ModelObject = TypeVar('ModelObject', bound=Model)
