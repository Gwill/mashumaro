# noinspection PyUnresolvedReferences
import builtins  # noqa
import collections
import collections.abc
import datetime
import enum
import os
import pathlib
import typing
import uuid

# noinspection PyUnresolvedReferences
from base64 import decodebytes, encodebytes  # noqa
from contextlib import contextmanager, suppress

# noinspection PyProtectedMember
from dataclasses import _FIELDS, MISSING, Field, is_dataclass
from decimal import Decimal
from fractions import Fraction

# noinspection PyUnresolvedReferences
from mashumaro.exceptions import (  # noqa
    InvalidFieldValue,
    MissingField,
    UnserializableDataError,
    UnserializableField,
)
from mashumaro.meta.helpers import (
    get_imported_module_names,
    get_type_origin,
    is_class_var,
    is_generic,
    is_init_var,
    is_special_typing_primitive,
    is_type_var,
    is_union,
    type_name,
)
from mashumaro.meta.patch import patch_fromisoformat
from mashumaro.serializer.base.helpers import *  # noqa
from mashumaro.types import SerializableType, SerializationStrategy

patch_fromisoformat()


NoneType = type(None)
INITIAL_MODULES = get_imported_module_names()


class CodeBuilder:
    def __init__(self, cls):
        self.cls = cls
        self.lines: typing.List[str] = []
        self.modules: typing.Set[str] = set()
        self.globals: typing.Set[str] = set()
        self._current_indent: str = ""

    def reset(self) -> None:
        self.lines = []
        self.modules = INITIAL_MODULES.copy()
        self.globals = set()
        self._current_indent = ""

    @property
    def namespace(self) -> typing.Dict[typing.Any, typing.Any]:
        return self.cls.__dict__

    @property
    def annotations(self) -> typing.Dict[str, typing.Any]:
        return self.namespace.get("__annotations__", {})

    def __get_field_types(
        self, recursive=True
    ) -> typing.Dict[str, typing.Any]:
        fields = {}
        for fname, ftype in typing.get_type_hints(self.cls).items():
            if is_class_var(ftype) or is_init_var(ftype):
                continue
            if recursive or fname in self.annotations:
                fields[fname] = ftype
        return fields

    @property
    def field_types(self) -> typing.Dict[str, typing.Any]:
        return self.__get_field_types()

    @property
    def defaults(self) -> typing.Dict[str, typing.Any]:
        d = {}
        for ancestor in self.cls.__mro__[-1:0:-1]:
            if is_dataclass(ancestor):
                for field in getattr(ancestor, _FIELDS).values():
                    if field.default is not MISSING:
                        d[field.name] = field.default
                    else:
                        d[field.name] = field.default_factory
        for name in self.__get_field_types(recursive=False):
            field = self.namespace.get(name, MISSING)
            if isinstance(field, Field):
                if field.default is not MISSING:
                    d[name] = field.default
                else:
                    # https://github.com/python/mypy/issues/6910
                    d[name] = field.default_factory  # type: ignore
            else:
                d[name] = field
        return d

    @property
    def metadatas(self) -> typing.Dict[str, typing.Mapping[str, typing.Any]]:
        d = {}
        for name in self.__get_field_types(recursive=False):
            field = self.namespace.get(name, MISSING)
            if isinstance(field, Field):
                d[name] = field.metadata
        return d

    def _add_type_modules(self, *types_) -> None:
        for t in types_:
            module = getattr(t, "__module__", None)
            if not module:
                return
            self.ensure_module_imported(module)
            args = getattr(t, "__args__", ())
            if args:
                self._add_type_modules(*args)
            constraints = getattr(t, "__constraints__", ())
            if constraints:
                self._add_type_modules(*constraints)

    def ensure_module_imported(self, module: str) -> None:
        if module not in self.modules:
            self.modules.add(module)
            self.add_line(f"if '{module}' not in globals():")
            with self.indent():
                self.add_line(f"import {module}")
            root_module = module.split(".")[0]
            if root_module not in self.globals:
                self.globals.add(root_module)
                self.add_line("else:")
                with self.indent():
                    self.add_line(f"global {root_module}")

    def add_line(self, line) -> None:
        self.lines.append(f"{self._current_indent}{line}")

    @contextmanager
    def indent(self) -> typing.Generator[None, None, None]:
        self._current_indent += " " * 4
        try:
            yield
        finally:
            self._current_indent = self._current_indent[:-4]

    def compile(self) -> None:
        exec("\n".join(self.lines), globals(), self.__dict__)

    def add_from_dict(self) -> None:

        self.reset()
        self.add_line("@classmethod")
        self.add_line(
            "def from_dict(cls, d, use_bytes=False, use_enum=False, "
            "use_datetime=False):"
        )
        with self.indent():
            self.add_line("try:")
            with self.indent():
                self.add_line("kwargs = {}")
                for fname, ftype in self.field_types.items():
                    metadata = self.metadatas.get(fname)
                    self._add_type_modules(ftype)
                    self.add_line(f"value = d.get('{fname}', MISSING)")
                    self.add_line("if value is None:")
                    with self.indent():
                        self.add_line(f"kwargs['{fname}'] = None")
                    self.add_line("else:")
                    with self.indent():
                        if self.defaults[fname] is MISSING:
                            self.add_line("if value is MISSING:")
                            with self.indent():
                                if isinstance(ftype, SerializationStrategy):
                                    self.add_line(
                                        f"raise MissingField('{fname}',"
                                        f"{type_name(ftype.__class__)},cls)"
                                    )
                                else:
                                    self.add_line(
                                        f"raise MissingField('{fname}',"
                                        f"{type_name(ftype)},cls)"
                                    )
                            self.add_line("else:")
                            with self.indent():
                                unpacked_value = self._unpack_field_value(
                                    fname=fname,
                                    ftype=ftype,
                                    parent=self.cls,
                                    metadata=metadata,
                                )
                                self.add_line("try:")
                                with self.indent():
                                    self.add_line(
                                        f"kwargs['{fname}'] = {unpacked_value}"
                                    )
                                self.add_line("except Exception as e:")
                                with self.indent():
                                    if isinstance(
                                        ftype, SerializationStrategy
                                    ):
                                        field_type = type_name(ftype.__class__)
                                    else:
                                        field_type = type_name(ftype)
                                    self.add_line(
                                        f"raise InvalidFieldValue('{fname}',"
                                        f"{field_type},value,cls)"
                                    )
                        else:
                            self.add_line("if value is not MISSING:")
                            with self.indent():
                                unpacked_value = self._unpack_field_value(
                                    fname=fname,
                                    ftype=ftype,
                                    parent=self.cls,
                                    metadata=metadata,
                                )
                                self.add_line("try:")
                                with self.indent():
                                    self.add_line(
                                        f"kwargs['{fname}'] = {unpacked_value}"
                                    )
                                self.add_line("except Exception as e:")
                                with self.indent():
                                    if isinstance(
                                        ftype, SerializationStrategy
                                    ):
                                        field_type = type_name(ftype.__class__)
                                    else:
                                        field_type = type_name(ftype)
                                    self.add_line(
                                        f"raise InvalidFieldValue('{fname}',"
                                        f"{field_type},value,cls)"
                                    )
            self.add_line("except AttributeError:")
            with self.indent():
                self.add_line("if not isinstance(d, dict):")
                with self.indent():
                    self.add_line(
                        f"raise ValueError('Argument for "
                        f"{type_name(self.cls)}.from_dict method "
                        f"should be a dict instance') from None"
                    )
                self.add_line("else:")
                with self.indent():
                    self.add_line("raise")
            self.add_line("return cls(**kwargs)")
        self.add_line("setattr(cls, 'from_dict', from_dict)")
        self.compile()

    def add_to_dict(self) -> None:

        self.reset()
        self.add_line(
            "def to_dict(self, use_bytes=False, use_enum=False, "
            "use_datetime=False):"
        )
        with self.indent():
            self.add_line("kwargs = {}")
            for fname, ftype in self.field_types.items():
                self.add_line(f"value = getattr(self, '{fname}')")
                self.add_line("if value is None:")
                with self.indent():
                    self.add_line(f"kwargs['{fname}'] = None")
                self.add_line("else:")
                with self.indent():
                    packed_value = self._pack_value(fname, ftype, self.cls)
                    self.add_line(f"kwargs['{fname}'] = {packed_value}")
            self.add_line("return kwargs")
        self.add_line("setattr(cls, 'to_dict', to_dict)")
        self.compile()

    def _pack_value(self, fname, ftype, parent, value_name="value"):

        if is_dataclass(ftype):
            return f"{value_name}.to_dict(use_bytes, use_enum, use_datetime)"

        with suppress(TypeError):
            if issubclass(ftype, SerializableType):
                return f"{value_name}._serialize()"
        if isinstance(ftype, SerializationStrategy):
            return (
                f"self.__dataclass_fields__['{fname}'].type"
                f"._serialize({value_name})"
            )

        origin_type = get_type_origin(ftype)
        if is_special_typing_primitive(origin_type):
            if origin_type is typing.Any:
                return value_name
            elif is_union(ftype):
                args = getattr(ftype, "__args__", ())
                if len(args) == 2 and args[1] == NoneType:  # it is Optional
                    return self._pack_value(fname, args[0], parent)
                else:
                    raise UnserializableDataError(
                        "Unions are not supported by mashumaro"
                    )
            elif origin_type is typing.AnyStr:
                raise UnserializableDataError(
                    "AnyStr is not supported by mashumaro"
                )
            elif is_type_var(ftype):
                raise UnserializableDataError(
                    "TypeVars are not supported by mashumaro"
                )
            else:
                raise UnserializableDataError(
                    f"{ftype} as a field type is not supported by mashumaro"
                )
        elif issubclass(origin_type, typing.Collection):
            args = getattr(ftype, "__args__", ())

            def inner_expr(arg_num=0, v_name="value"):
                return self._pack_value(fname, args[arg_num], parent, v_name)

            if issubclass(
                origin_type,
                (typing.List, typing.Deque, typing.Tuple, typing.AbstractSet),
            ):
                if is_generic(ftype):
                    return f"[{inner_expr()} for value in {value_name}]"
                elif ftype is list:
                    raise UnserializableField(
                        fname, ftype, parent, "Use typing.List[T] instead"
                    )
                elif ftype is collections.deque:
                    raise UnserializableField(
                        fname, ftype, parent, "Use typing.Deque[T] instead"
                    )
                elif ftype is tuple:
                    raise UnserializableField(
                        fname, ftype, parent, "Use typing.Tuple[T] instead"
                    )
                elif ftype is set:
                    raise UnserializableField(
                        fname, ftype, parent, "Use typing.Set[T] instead"
                    )
                elif ftype is frozenset:
                    raise UnserializableField(
                        fname, ftype, parent, "Use typing.FrozenSet[T] instead"
                    )
            elif issubclass(origin_type, typing.ChainMap):
                if ftype is collections.ChainMap:
                    raise UnserializableField(
                        fname,
                        ftype,
                        parent,
                        "Use typing.ChainMap[KT,VT] instead",
                    )
                elif is_generic(ftype):
                    if is_dataclass(args[0]):
                        raise UnserializableDataError(
                            "ChainMaps with dataclasses as keys "
                            "are not supported by mashumaro"
                        )
                    else:
                        return (
                            f'[{{{inner_expr(0,"key")}:{inner_expr(1)} '
                            f"for key,value in m.items()}} "
                            f"for m in value.maps]"
                        )
            elif issubclass(origin_type, typing.Mapping):
                if ftype is dict:
                    raise UnserializableField(
                        fname,
                        ftype,
                        parent,
                        "Use typing.Dict[KT,VT] or Mapping[KT,VT] instead",
                    )
                elif is_generic(ftype):
                    if is_dataclass(args[0]):
                        raise UnserializableDataError(
                            "Mappings with dataclasses as keys "
                            "are not supported by mashumaro"
                        )
                    else:
                        return (
                            f'{{{inner_expr(0,"key")}: {inner_expr(1)} '
                            f"for key, value in {value_name}.items()}}"
                        )
            elif issubclass(origin_type, typing.ByteString):
                return (
                    f"{value_name} if use_bytes else "
                    f"encodebytes({value_name}).decode()"
                )
            elif issubclass(origin_type, str):
                return value_name
            elif issubclass(origin_type, typing.Sequence):
                if is_generic(ftype):
                    return f"[{inner_expr()} for value in {value_name}]"
        elif issubclass(origin_type, os.PathLike):
            return f"{value_name}.__fspath__()"
        elif issubclass(origin_type, enum.Enum):
            return f"{value_name} if use_enum else {value_name}.value"
        elif origin_type is int:
            return f"int({value_name})"
        elif origin_type is float:
            return f"float({value_name})"
        elif origin_type in (bool, NoneType):
            return value_name
        elif origin_type in (datetime.datetime, datetime.date, datetime.time):
            return (
                f"{value_name} if use_datetime else {value_name}.isoformat()"
            )
        elif origin_type is datetime.timedelta:
            return f"{value_name}.total_seconds()"
        elif origin_type is datetime.timezone:
            return f"{value_name}.tzname(None)"
        elif origin_type is uuid.UUID:
            return f"str({value_name})"
        elif origin_type is Decimal:
            return f"str({value_name})"
        elif origin_type is Fraction:
            return f"str({value_name})"

        raise UnserializableField(fname, ftype, parent)

    def _unpack_field_value(
        self, fname, ftype, parent, value_name="value", metadata=None
    ):

        if is_dataclass(ftype):
            return (
                f"{type_name(ftype)}.from_dict({value_name}, "
                f"use_bytes, use_enum, use_datetime)"
            )

        with suppress(TypeError):
            if issubclass(ftype, SerializableType):
                return f"{type_name(ftype)}._deserialize({value_name})"
        if isinstance(ftype, SerializationStrategy):
            return (
                f"cls.__dataclass_fields__['{fname}'].type"
                f"._deserialize({value_name})"
            )

        origin_type = get_type_origin(ftype)
        if is_special_typing_primitive(origin_type):
            if origin_type is typing.Any:
                return value_name
            elif is_union(ftype):
                args = getattr(ftype, "__args__", ())
                if len(args) == 2 and args[1] == NoneType:  # it is Optional
                    return self._unpack_field_value(fname, args[0], parent)
                else:
                    raise UnserializableDataError(
                        "Unions are not supported by mashumaro"
                    )
            elif origin_type is typing.AnyStr:
                raise UnserializableDataError(
                    "AnyStr is not supported by mashumaro"
                )
            elif is_type_var(ftype):
                raise UnserializableDataError(
                    "TypeVars are not supported by mashumaro"
                )
            else:
                raise UnserializableDataError(
                    f"{ftype} as a field type is not supported by mashumaro"
                )
        elif issubclass(origin_type, typing.Collection):
            args = getattr(ftype, "__args__", ())

            def inner_expr(arg_num=0, v_name="value"):
                return self._unpack_field_value(
                    fname, args[arg_num], parent, v_name
                )

            if issubclass(origin_type, typing.List):
                if is_generic(ftype):
                    return f"[{inner_expr()} for value in {value_name}]"
                elif ftype is list:
                    raise UnserializableField(
                        fname, ftype, parent, "Use typing.List[T] instead"
                    )
            elif issubclass(origin_type, typing.Deque):
                if is_generic(ftype):
                    return (
                        f"collections.deque([{inner_expr()} "
                        f"for value in {value_name}])"
                    )
                elif ftype is collections.deque:
                    raise UnserializableField(
                        fname, ftype, parent, "Use typing.Deque[T] instead"
                    )
            elif issubclass(origin_type, typing.Tuple):
                if is_generic(ftype):
                    return f"tuple([{inner_expr()} for value in {value_name}])"
                elif ftype is tuple:
                    raise UnserializableField(
                        fname, ftype, parent, "Use typing.Tuple[T] instead"
                    )
            elif issubclass(origin_type, typing.FrozenSet):
                if is_generic(ftype):
                    return (
                        f"frozenset([{inner_expr()} "
                        f"for value in {value_name}])"
                    )
                elif ftype is frozenset:
                    raise UnserializableField(
                        fname, ftype, parent, "Use typing.FrozenSet[T] instead"
                    )
            elif issubclass(origin_type, typing.AbstractSet):
                if is_generic(ftype):
                    return f"set([{inner_expr()} for value in {value_name}])"
                elif ftype is set:
                    raise UnserializableField(
                        fname, ftype, parent, "Use typing.Set[T] instead"
                    )
            elif issubclass(origin_type, typing.ChainMap):
                if ftype is collections.ChainMap:
                    raise UnserializableField(
                        fname,
                        ftype,
                        parent,
                        "Use typing.ChainMap[KT,VT] instead",
                    )
                elif is_generic(ftype):
                    if is_dataclass(args[0]):
                        raise UnserializableDataError(
                            "ChainMaps with dataclasses as keys "
                            "are not supported by mashumaro"
                        )
                    else:
                        return (
                            f"collections.ChainMap("
                            f'*[{{{inner_expr(0,"key")}:{inner_expr(1)} '
                            f"for key, value in m.items()}} "
                            f"for m in {value_name}])"
                        )
            elif issubclass(origin_type, typing.Mapping):
                if ftype is dict:
                    raise UnserializableField(
                        fname,
                        ftype,
                        parent,
                        "Use typing.Dict[KT,VT] or Mapping[KT,VT] instead",
                    )
                elif is_generic(ftype):
                    if is_dataclass(args[0]):
                        raise UnserializableDataError(
                            "Mappings with dataclasses as keys "
                            "are not supported by mashumaro"
                        )
                    else:
                        return (
                            f'{{{inner_expr(0,"key")}: {inner_expr(1)} '
                            f"for key, value in {value_name}.items()}}"
                        )
            elif issubclass(origin_type, typing.ByteString):
                if origin_type is bytes:
                    return (
                        f"{value_name} if use_bytes else "
                        f"decodebytes({value_name}.encode())"
                    )
                elif origin_type is bytearray:
                    return (
                        f"bytearray({value_name} if use_bytes else "
                        f"decodebytes({value_name}.encode()))"
                    )
            elif issubclass(origin_type, str):
                return value_name
            elif issubclass(origin_type, typing.Sequence):
                if is_generic(ftype):
                    return f"[{inner_expr()} for value in {value_name}]"
        elif issubclass(origin_type, os.PathLike):
            if issubclass(origin_type, pathlib.PosixPath):
                return f"pathlib.PosixPath({value_name})"
            elif issubclass(origin_type, pathlib.WindowsPath):
                return f"pathlib.WindowsPath({value_name})"
            elif issubclass(origin_type, pathlib.Path):
                return f"pathlib.Path({value_name})"
            elif issubclass(origin_type, pathlib.PurePosixPath):
                return f"pathlib.PurePosixPath({value_name})"
            elif issubclass(origin_type, pathlib.PureWindowsPath):
                return f"pathlib.PureWindowsPath({value_name})"
            elif issubclass(origin_type, pathlib.PurePath):
                return f"pathlib.PurePath({value_name})"
            elif origin_type is os.PathLike:
                return f"pathlib.PurePath({value_name})"
            else:
                return f"{type_name(origin_type)}({value_name})"
        elif issubclass(origin_type, enum.Enum):
            return (
                f"{value_name} if use_enum "
                f"else {type_name(origin_type)}({value_name})"
            )
        elif origin_type is int:
            return f"int({value_name})"
        elif origin_type is float:
            return f"float({value_name})"
        elif origin_type in (bool, NoneType):
            return value_name
        elif origin_type in (datetime.datetime, datetime.date, datetime.time):
            if metadata is not None:
                deserialize = metadata.get("deserialize")
                datetime_parser: typing.Optional[str] = None
                if deserialize == "ciso8601":
                    self.ensure_module_imported("ciso8601")
                    datetime_parser = "ciso8601.parse_datetime"
                elif deserialize == "pendulum":
                    self.ensure_module_imported("pendulum")
                    datetime_parser = "pendulum.parse"
                elif callable(deserialize):
                    self.ensure_module_imported(deserialize.__module__)
                    try:
                        datetime_parser = (
                            f"{deserialize.__module__}."
                            f"{deserialize.__qualname__}"
                        )
                    except AttributeError:
                        if hasattr(deserialize, "__dict__"):
                            raise UnserializableField(
                                fname,
                                ftype,
                                parent,
                                "Using callable class instances in field "
                                "metadata deserialize option isn't supported "
                                "yet",
                            )
                if not datetime_parser:
                    raise UnserializableField(
                        fname,
                        ftype,
                        parent,
                        f"Unsupported field metadata deserialize "
                        f"option value: {deserialize}",
                    )
                else:
                    suffix = ""
                    if not callable(deserialize):
                        if origin_type is datetime.date:
                            suffix = ".date()"
                        elif origin_type is datetime.time:
                            suffix = ".time()"
                    return (
                        f"{value_name} if use_datetime else "
                        f"{datetime_parser}({value_name}){suffix}"
                    )
            return (
                f"{value_name} if use_datetime else "
                f"datetime.{origin_type.__name__}."
                f"fromisoformat({value_name})"
            )
        elif origin_type is datetime.timedelta:
            return f"datetime.timedelta(seconds={value_name})"
        elif origin_type is datetime.timezone:
            return f"parse_timezone({value_name})"
        elif origin_type is uuid.UUID:
            return f"uuid.UUID({value_name})"
        elif origin_type is Decimal:
            return f"Decimal({value_name})"
        elif origin_type is Fraction:
            return f"Fraction({value_name})"

        raise UnserializableField(fname, ftype, parent)
