"""
The :mod:`findig.tools.validator` module exposes the :class:`Validator`
which can be used to validate an application or request's input data.

Validators work by specifying a converter for each field in the 
input data to be validated::

    validator = Validator(app)
    
    @validator.enforce(id=int)
    @app.route("/test")
    def resource():
        pass

    @resource.model("write")
    def write_resource(data):
        assert isinstance(data['id'], int)

If the converter fails to convert the field's value, then a 
``400 BAD REQUEST`` error is sent back.

Converters don't have to be functions; they can be a singleton list
containing another converter, indicating that the field is expected to
be a list of items for which that converter works::

    @validator.enforce(ids=[int])
    @app.route("/test2")
    def resource2():
        pass

    @resource2.model("write")
    def write_resource(data):
        for id in data['ids']:
            assert isinstance(id, int)

Converters can also be string specifications corresponding to a 
pre-registered converter and its arguments. All of
werkzeug's 
`builtin converters and their arguments`__ 
and their arguments are pre-registered and thus usable::

    @validator.enforce(foo='any(bar,baz)', cid='string(length=3)')
    @app.route("/test3")
    def resource3():
        pass

    @resource3.model("write")
    def write_resource(data):
        assert data['foo'] in ('bar', 'baz')
        assert len(data['cid']) == 3

__ http://werkzeug.pocoo.org/docs/routing/#builtin-converters

"""

from collections import namedtuple
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from functools import partial
from inspect import getmembers
import re

from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import BadRequest
from werkzeug.routing import parse_converter_args, BaseConverter

from findig.context import ctx
from findig.resource import AbstractResource, Collection
from findig.utils import DataPipe, tryeach


_converter_re = re.compile(r'''
        (?P<name>[a-zA-Z_][a-zA-Z0-9_]*)   # converter name
        (?:\((?P<args>.*?)\))?                  # converter args
''', re.VERBOSE | re.UNICODE)

class converter_spec(namedtuple('converter_spec', 'name args')):
    def __repr__(self):
        if self.args is None:
            return repr(self.name)
        else:
            return repr("{}({})".format(self.name, self.args))

class Validator:
    """
    A higher-level tool to be used to validate request input data.

    :param app: The Findig application that the validator is attached to.
    :type app: :class:`findig.App`
    :param include_collections: If ``True``, any validation rules set on any
        resource will also be used for any :class:`~findig.resource.Collection`
        that collects it. Even when this argument is set, inherited rules can still
        be overridden by declaring rules specifically for the collection.

    Validators are only capable of validating request input data (i.e., 
    data received as part of the request body). To validate URL fragments,
    consider using *converters* in your URL rules. See 
    `werkzeug's routing reference <http://werkzeug.pocoo.org/docs/0.10/routing/#rule-format>`_.

    Validators work by specifying converters for request input fields.
    If a converter is specified, the validator will use it to convert the
    field and replace it with the converted value. See :meth:`enforce` for
    more about converters.

    """
    def __init__(self, app=None, include_collections=True):
        self.include_collections = include_collections
        self.validation_specs = {}
        self.restriction_specs = {}
        self.strip_extras = {}

        if app is not None:
            self.attach_to(app)

    def attach_to(self, app):
        """
        Hook the validator into a Findig application.

        Doing so allows the validator to inspect and replace incoming
        input data. This is called automatically for an app passed to the
        validator's constructor, but can be called for additional app
        instances. This function should only be called once per application.

        :param app: The Findig application that the validator is attached to.
        :type app: :class:`findig.App`
        """
        # Hook the validator into the dispatcher's data pre processor
        # so that we can look at incoming request data and complain
        # if the request data doesn't match what we're looking for
        app.pre_processor = DataPipe(app.pre_processor, self.validate)
        app.startup_hook(partial(self.__prepare_converters, app))

    @staticmethod
    def regex(pattern, flags=0, template=None):
        r"""
        Create a function that validates strings against a regular expression.

        ::

            >>> func = Validator.regex("boy")
            >>> func("boy")
            'boy'
            >>> func("That boy")
            Traceback (most recent call last):
              ...
            ValueError: That boy
            >>> func("boy, that's handy.")
            Traceback (most recent call last):
              ...
            ValueError: boy, that's handy.

        If you supply a template, it is used to construct a return 
        value by doing backslash substitution::

            >>> func = Validator.regex("(male|female)", template=r"Gender: \1")
            >>> func("male")
            'Gender: male'
            >>> func("alien")
            Traceback (most recent call last):
              ...
            ValueError: alien
            
        """

        regexp = re.compile(pattern, flags)
        def match_string(s):
            m = regexp.fullmatch(s)
            if m is None:
                raise ValueError(s)
            elif template is not None:
                return m.expand(template)
            else:
                return m.string
        return match_string

    @staticmethod
    def date(format, *alternatives):
        """
        date(format[, format[, ...]])

        Create a function that validates a date field.

        :param format: A date/time format according to 
            :meth:`datetime.datetime.strptime`. If more than one formats are
            passed in, the generated function will try each format in order
            until one of them works on the field (or until there are no formats
            left to try).

        Example::

            >>> func = Validator.date("%Y-%m-%d %H:%M:%S%z")
            >>> func("2015-07-17 09:00:00+0400")
            datetime.datetime(2015, 7, 17, 9, 0, tzinfo=datetime.timezone(datetime.timedelta(0, 14400)))
            >>> func("not-a-date")
            Traceback (most recent call last):
              ...
            ValueError: time data 'not-a-date' does not match format '%Y-%m-%d %H:%M:%S%z'

            >>> func = Validator.date("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d")
            >>> func("2015-07-17")
            datetime.datetime(2015, 7, 17, 0, 0)

        """
        formats = [format]; formats.extend(alternatives)
        funcs = [lambda s: datetime.strptime(s, fmt) for fmt in formats]
        return partial(tryeach, funcs)

    def restrict(self, *args, strip_extra=False):
        """
        restrict([field[, field[, ...]],] strip_extra=True)
        Restrict the input data to the given fields

        :param field: A field name that should be allowed. An asterisk at the
            start of the field name indicates a required field (asterisks at
            the start of field names can be escaped with another asterisk
            character). This parameter can be used multiple times to indicate
            different fields.

        :param strip_extra: Controls the behavior upon encountering a field
            not contained in the list, during validation. If ``True``, the
            field will be removed. Otherwise, a :class:`UnexpectedFields` is
            raised.

        Once this method is called, any field names that do not appear in the
        list are disallowed.

        """
        FIELD_REGEX = re.compile(r"^(\**)\1(\*?)")
        def conv_field(field_name):
            """conv_field("*field") -> ("field", FIELD-IS-REQUIRED)"""
            m = FIELD_REGEX.match(field_name)
            if m:
                return (FIELD_REGEX.sub(r"\1", field_name), m.group(2) == "*")
            else:
                return field_name, False

        def restrict_to(resource, fields=None):
            fields = list(map(conv_field, [] if fields is None else fields))
            self.restriction_specs.setdefault(resource.name, {}).update(fields)
            self.strip_extras[resource.name] = strip_extra
            return resource

        if len(args) == 0:
            return restrict_to

        elif isinstance(args[0], AbstractResource):
            return restrict_to(args[0], args[1:])

        else:
            return partial(restrict_to, fields=args)

    def enforce(self, *args, **validator_spec):
        """
        enforce(resource, **validation_spec)

        Register a validation specification for a resource.

        The validation specification is a set of ``field=converter``
        arguments linking an input field name to a converter that should
        be used to validate the field. A converter can be any of the following:

        *   :class:`collections.abc.Callable` (including functions) -- This can
            be a simple type such as :class:`int` or :class:`uuid.UUID`, but
            any function or callable can work. It should take a field value and 
            convert it to a value of the desired type. If it throws an error,
            then findig will raise a :class:`~werkzeug.exceptions.BadRequest` 
            exception.

            Example::
       
                # Converts an int from a valid string base 10 representation:
                validator.enforce(resource, game_id=int)

                # Converts to a float from a valid string
                validator.enforce(resource, duration=float)

        *   :class:`str` -- If a string is given, then it is interpreted as a
            converter specification. A converter specification includes the
            converter name and optionally arguments for pre-registered
            converters. The following converters are pre-registered by
            default (you may notice that they correspond to the URL rule
            converters available for werkzeug):

            .. function:: string(minlength=1, length=None, maxlength=None)
                :noindex:

                This converter will accept a string.
            
                :param length: If given, it will indicate a fixed length field.
                :param minlength: The minimum allowed length for the field.
                :param maxlength: The maximum allowed length for the field.

            .. function:: any(*items)
                :noindex:

                This converter will accept only values from the variable
                list of options passed as the converter arguments. It's
                useful for limiting a field's value to a small set of possible
                options.

            .. function:: int(fixed_digits=0, min=None, max=None)
                :noindex:

                This converter will accept a string representation of a
                non-negative integer.

                :param fixed_digits: The number of fixed digits in the field.
                    For example, set this to **3** to convert ``'001'`` but not
                    ``'1'``. The default is a variable number of digits.
                :param min: The minimum allowed value for the field.
                :param max: The maximum allowed value for the field.

            .. function:: float(min=None, max=None)
                :noindex:

                This converter will accept a string representation of a
                non-negative floating point number.

                :param min: The minimum allowed value for the field.
                :param max: The maximum allowed value for the field.

            .. function:: uuid()
                :noindex:

                This converter will accept a string representation of a
                uuid and convert it to a :class:`uuid.UUID`.

            Converters that do not need arguments can omit the parentheses
            in the converter specification.

            Examples::

                # Converts a 4 character string
                validator.enforce(resource, student_id='string(length=10)')

                # Converts any of these string values: 'foo', 1000, True
                validator.enforce(resource, field='any(foo, 1000, True)')

                # Converts any non-negative integer
                validator.enforce(resource, game_id='int')

                # and any float <1000
                validator.enforce(resource, duration='float(max=1000)')

            .. important:: Converter specifications in this form **cannot**
               match strings that contain forward slashes. For example,
               *'string(length=2)'* will fail to match *'/e'* and 
               *'any(application/json,html)'* will fail to
               match *'application/json'*.

        *   or, :class:`list` -- This must be a singleton list containing a
            converter. When this is given, the validator will treat the field
            like a list and use the converter to convert each item.

            Example::

                # Converts a list of integers
                validator.enforce(resource, games=[int])

                # Converts a list of uuids
                validator.enforce(resource, components=['uuid'])

                # Converts a list of fixed length strings
                validator.enforce(resource, students=['string(length=10)'])

        This method can be used as a decorator factory for resources::

            @validator.enforce(uid=int, friends=[int])
            @app.route("/")
            def res():
                return {}

        Converter specifications given here are only checked when a field is
        present; see :meth:`restrict` for specifying required fields.

        .. warning:: Because of the way validators are hooked up, registering
            new specifications after the first request has run might cause
            unexpected behavior (and even internal server errors).

        """
        def decorator(resource):
            self.__register_spec(resource.name, validator_spec)
            return resource

        if len(args) == 0:
            return decorator
        elif len(args) == 1:
            return decorator(args[0])
        else:
            raise TypeError

    def enforce_all(self, **validator_spec):
        """
        enforce_all(**validation_spec)

        Register a global validation specification.

        This function works like :meth:`enforce`, except that the
        validation specification is registered for all resources instead
        of a single one.

        Global validation specifications have lower precedence than
        resource specific ones.

        """
        
        self.__register_spec(None, validator_spec)

    def __register_spec(self, key, spec):
        def validate_item_spec(item_spec):
            if isinstance(item_spec, str):
                m = _converter_re.fullmatch(item_spec)
                if m is not None:
                    return converter_spec(m.group('name'), m.group('args'))
                else:
                    return False

            elif isinstance(item_spec, list):
                if len(item_spec) != 1:
                    return False
                else:
                    return [validate_item_spec(item_spec[0])]

            elif not isinstance(item_spec, Callable):
                return False

            else:
                return item_spec

        for field, item_spec in spec.items():
            new_item_spec = validate_item_spec(item_spec)
            if item_spec is False:
                raise InvalidSpecificationError(item_spec)
            else:
                spec[field] = new_item_spec

        else:
            self.validation_specs.setdefault(key, {})
            self.validation_specs[key].update(spec)

    def __prepare_converters(self, app):
        def fix_spec(item_spec):
            if isinstance(item_spec, converter_spec):
                cname, args = item_spec
                args, kwargs = ((), {}) if args is None \
                                        else parse_converter_args(args)
                if cname not in app.url_map.converters:
                    return None

                ccls = app.url_map.converters[cname]
                converter = ccls(app.url_map, *args, **kwargs)                   

                compiled_re = re.compile(converter.regex)
                return compiled_re, converter
            elif isinstance(item_spec, list):
                return [fix_spec(item_spec[0])]
            else:
                return item_spec

        for resource in self.validation_specs:
            for field, spec in self.validation_specs[resource].items():
                new_spec = fix_spec(spec)
                if new_spec is not None:
                    self.validation_specs[resource][field] = new_spec
                else:
                    raise InvalidSpecificationError(
                        "\"{}={!r}\"".format(field, spec)
                    )
                
    def __check_item(self, data, key, item_spec):
        # '89', int -> pass
        # ['58', '84', '58'], [int] -> pass
        # ['89', 'foo', '59'], [int] -> fail
        # ['89', 'foo', '59'], [str] -> pass
        if isinstance(item_spec, Callable):
            # Easiest case: call the callable on the item data
            # to get the converted answer:
            return item_spec(data[key])

        elif isinstance(item_spec, list):
            unconverted_items = _ContainerWrapper(data.getlist(key))
            converted_items = []
            child_spec = item_spec[0]
            for i, _ in enumerate(unconverted_items):
                converted_items.append(
                    self.__check_item(unconverted_items, i, child_spec)
                )
            return converted_items
        elif isinstance(item_spec, tuple):
            regexp, converter = item_spec
            val = data[key]
            if not regexp.fullmatch(val):
                raise ValueError(val)
            else:
                return converter.to_python(val)

        else:
            raise InvalidSpecificationError(item_spec)

    def __handle_restrictions(self, data):
        strip_extras =  self.strip_extras.get(ctx.resource.name, False)
        restrictions = self.restriction_specs.get(ctx.resource.name, None)
        if restrictions is None \
        and self.include_collections \
        and isinstance(ctx.resource, Collection):
            restrictions = self.restriction_specs.get(
                ctx.resource.collects.resource.name,
                {}
            )
            strip_extras =  self.strip_extras.get(
                ctx.resource.collects.resource.name,
                False
            )
        if restrictions is not None:
            # Handle extra fields
            extras = [field for field in data if field not in restrictions]
            if extras and strip_extras:
                for field in extras:
                    del data[field]
            elif extras:
                raise UnexpectedFields(extras, self)

            # Check for required fields
            missing = [field for field,required in restrictions.items()
                       if required and field not in data]
            if missing:
                raise MissingFields(missing, self)

        return data

    def validate(self, data):
        """
        Validate the data with the validation specifications that have
        been collected.

        This function must be called within an active request context in
        order to work.

        :param data: Input data
        :type data: mapping, or object with gettable/settable fields
        :raises: :class:`ValidationFailed` if one or more fields could not be
            validated.

        **This is an internal method.**

        """
        spec = {}
        spec.update(self.validation_specs.get(None, {}))
        if self.include_collections and isinstance(ctx.resource, Collection):
            spec.update(
                self.validation_specs.get(
                    ctx.resource.collects.resource.name,
                    {}
                )
            )
        spec.update(self.validation_specs.get(ctx.resource.name, {}))

        wrapped = self.__handle_restrictions(_ContainerWrapper(data))

        conversion_errs = []

        # Transform the data according to the conversion spec
        for field, field_spec in spec.items():
            # Ignore the field if it isn't in the specification
            if field not in wrapped:
                continue

            try:
                converted = self.__check_item(wrapped, field, field_spec)
            except InvalidSpecificationError:
                raise
            except:
                import traceback
                traceback.print_exc()
                conversion_errs.append(field)
            else:
                wrapped[field] = converted

        if conversion_errs:
            raise InvalidFields(conversion_errs, self)
        else:
            return wrapped.unwrap()

class _ContainerWrapper:
    def __init__(self, container):  
        self._direct = False
        self._orig = container
        self._is_multidict = False
            
        if isinstance(container, Sequence):
            self._c = list(container)

        elif isinstance(container, MultiDict):
            self._is_multidict = True
            self._list_fields = set()
            self._c = container.copy()

        elif isinstance(container, Mapping):
            self._c = dict(container)

        else:
            self._c = container
            self._direct = True

    def __getitem__(self, key):
        if self._direct:
            return getattr(self._c, key)
        else:
            return self._c[key]

    def getlist(self, key):
        if self._is_multidict:
            l = self._c.getlist(key)
            self._list_fields.add(key)
            return l
        else:
            l = self[key]
            if not isinstance(l, Sequence):
                raise ValueError(l)
            else:
                return l

    def __setitem__(self, key, value):
        if self._direct:
            setattr(self._c, key, value)
        else:
            self._c[key] = value

    def __delitem__(self, key):
        if self._direct:
            delattr(self._c, key)
        else:
            del self._c[key]

    def __contains__(self, key):
        if self._direct:
            return hasattr(self._c, key)
        else:
            return key in self._c

    def unwrap(self):
        if isinstance(self._c, MultiDict):
            for field in self._list_fields:
                items = self._c.get(field, [])
                self._c.setlist(field, items)

        return self._c

    def __iter__(self):
        if self._direct:
            yield from getmembers(self._c)
        else:
            yield from self._c


class InvalidSpecificationError(ValueError):
    """Raised when an invalid specification is used."""

class ValidationFailed(BadRequest):
    """
    Raised whenever a :class:`Validator` fails to validate one or more 
    fields.

    This exception is a subclass of :class:`werkzeug.exceptions.BadRequest`, so if allowed
    to bubble up, findig will send a ``400 BAD REQUEST``
    response automatically.

    Applications can, however, customize the way this exception is 
    handled::

        from werkzeug.wrappers import Response

        # This assumes that the app was not supplied a custom error_handler
        # function as an argument.
        # If a custom error_handler function is being used, then
        # do a test for this exception type inside the function body
        # and replicate the logic
        @app.error_handler.register(ValidationFailed)
        def on_validation_failed(e):
            # Construct a response based on the error received
            msg = "Failed to convert input data for the following fields: "
            msg += str(e.fields)
            return Response(msg, status=e.status)
            
    """
    def __init__(self, fields, validator):
        super().__init__()

        #: A list of field names for which validation has failed. This will
        #: always be a complete list of failed fields.
        self.fields = fields

        #: The :class:`Validator` instance that raised the exception.
        self.validator = validator

class UnexpectedFields(ValidationFailed):
    """
    Raised whenever a resource receives an unexpected input field.
    """

class MissingFields(ValidationFailed):
    """
    Raised when a resource does not receive a required field in its input.
    """

class InvalidFields(ValidationFailed):
    """
    Raised when a resource receives a field that the validator can't convert.
    """



__all__ = ['Validator', 'InvalidSpecificationError', 'ValidationFailed', 
           'UnexpectedFields', 'MissingFields', 'InvalidFields']
