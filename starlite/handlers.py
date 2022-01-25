# pylint: disable=too-many-instance-attributes, too-many-arguments
from contextlib import suppress
from copy import copy
from enum import Enum
from inspect import Signature, isawaitable, isclass, ismethod
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Generator,
    List,
    Optional,
    Type,
    Union,
    cast,
)

from pydantic import validate_arguments
from pydantic.typing import AnyCallable
from starlette.background import BackgroundTask, BackgroundTasks
from starlette.requests import HTTPConnection
from starlette.responses import FileResponse, RedirectResponse
from starlette.responses import Response as StarletteResponse
from starlette.responses import StreamingResponse
from starlette.status import HTTP_200_OK, HTTP_201_CREATED, HTTP_204_NO_CONTENT

from starlite.constants import REDIRECT_STATUS_CODES
from starlite.controller import Controller
from starlite.datastructures import File, Redirect, StarliteType, Stream, Template
from starlite.enums import HttpMethod, MediaType
from starlite.exceptions import (
    HTTPException,
    ImproperlyConfiguredException,
    InternalServerException,
    ValidationException,
)
from starlite.plugins.base import PluginProtocol, get_plugin_for_value
from starlite.provide import Provide
from starlite.response import Response, TemplateResponse
from starlite.types import (
    AfterRequestHandler,
    AsyncAnyCallable,
    BeforeRequestHandler,
    Guard,
    Method,
    ResponseHeader,
)
from starlite.utils import SignatureModel, normalize_path

if TYPE_CHECKING:  # pragma: no cover
    from starlite.routing import Router


class _empty:
    """Placeholder"""


class BaseRouteHandler:
    __slots__ = (
        "paths",
        "dependencies",
        "guards",
        "opt",
        "fn",
        "owner",
        "resolved_dependencies",
        "resolved_guards",
        "signature_model",
    )

    @validate_arguments(config={"arbitrary_types_allowed": True})
    def __init__(
        self,
        path: Union[Optional[str], Optional[List[str]]] = None,
        dependencies: Optional[Dict[str, Provide]] = None,
        guards: Optional[List[Guard]] = None,
        opt: Optional[Dict[str, Any]] = None,
    ):
        self.paths: List[str] = (
            [normalize_path(p) for p in path]
            if path and isinstance(path, list)
            else [normalize_path(path or "/")]  # type: ignore
        )
        self.dependencies = dependencies
        self.guards = guards
        self.opt: Dict[str, Any] = opt or {}
        self.fn: Optional[AnyCallable] = None
        self.owner: Optional[Union[Controller, "Router"]] = None
        self.resolved_dependencies: Union[Dict[str, Provide], Type[_empty]] = _empty
        self.resolved_guards: Union[List[Guard], Type[_empty]] = _empty
        self.signature_model: Optional[Type[SignatureModel]] = None

    def ownership_layers(self) -> Generator[Union["BaseRouteHandler", Controller, "Router"], None, None]:
        """
        Returns all the handler and then all owners up to the app level

        handler -> ... -> App
        """
        cur: Any = self
        while cur:
            value = cur
            cur = cur.owner
            yield value

    def resolve_guards(self) -> List[Guard]:
        """Returns all guards in the handlers scope, starting from highest to current layer"""
        if self.resolved_guards is _empty:
            resolved_guards: List[Guard] = []
            for layer in self.ownership_layers():
                if layer.guards:
                    resolved_guards.extend(layer.guards)
            # we reverse the list to ensure that the highest level guards are called first
            self.resolved_guards = list(reversed(resolved_guards))
        return cast(List[Guard], self.resolved_guards)

    def resolve_dependencies(self) -> Dict[str, Provide]:
        """
        Returns all dependencies correlating to handler function's kwargs that exist in the handler's scope
        """
        if not self.signature_model:  # pragma: no cover
            raise RuntimeError("resolve_dependencies cannot be called before a signature model has been generated")
        if self.resolved_dependencies is _empty:
            dependencies: Dict[str, Provide] = {}
            for layer in self.ownership_layers():
                for key, value in (layer.dependencies or {}).items():
                    if key not in dependencies:
                        self.validate_dependency_is_unique(dependencies=dependencies, key=key, provider=value)
                        dependencies[key] = value
            self.resolved_dependencies = dependencies
        return cast(Dict[str, Provide], self.resolved_dependencies)

    @staticmethod
    def validate_dependency_is_unique(dependencies: Dict[str, Provide], key: str, provider: Provide) -> None:
        """
        Validates that a given provider has not been already defined under a different key
        """
        for dependency_key, value in dependencies.items():
            if provider == value:
                raise ImproperlyConfiguredException(
                    f"Provider for key {key} is already defined under the different key {dependency_key}. "
                    f"If you wish to override a provider, it must have the same key."
                )

    def validate_handler_function(self) -> None:
        """
        Validates the route handler function once it's set by inspecting its return annotations
        """
        if not self.fn:  # pragma: no cover
            raise ImproperlyConfiguredException("Cannot call validate_handler_function without first setting self.fn")

    async def authorize_connection(self, connection: HTTPConnection) -> None:
        """
        Ensures the connection is authorized by running all the route guards in scope
        """
        for guard in self.resolve_guards():
            result = guard(connection, copy(self))
            if isawaitable(result):
                await result


class HTTPRouteHandler(BaseRouteHandler):
    __slots__ = (
        "http_method",
        "status_code",
        "after_request",
        "background_tasks",
        "before_request",
        "media_type",
        "response_class",
        "response_headers",
        "content_encoding",
        "content_media_type",
        "deprecated",
        "description",
        "include_in_schema",
        "operation_id",
        "raises",
        "response_description",
        "summary",
        "tags",
        "resolved_headers",
        "resolved_response_class",
        "resolved_after_request",
        "resolved_before_request",
        "template_name",
    )

    @validate_arguments(config={"arbitrary_types_allowed": True})
    def __init__(
        self,
        path: Union[Optional[str], Optional[List[str]]] = None,
        http_method: Union[HttpMethod, Method, List[Union[HttpMethod, Method]]] = None,  # type: ignore
        after_request: Optional[AfterRequestHandler] = None,
        background_tasks: Optional[Union[BackgroundTask, BackgroundTasks]] = None,
        before_request: Optional[BeforeRequestHandler] = None,
        dependencies: Optional[Dict[str, Provide]] = None,
        guards: Optional[List[Guard]] = None,
        media_type: Union[MediaType, str] = MediaType.JSON,
        opt: Optional[Dict[str, Any]] = None,
        response_class: Optional[Type[Response]] = None,
        response_headers: Optional[Dict[str, ResponseHeader]] = None,
        status_code: Optional[int] = None,
        # OpenAPI related attributes
        content_encoding: Optional[str] = None,
        content_media_type: Optional[str] = None,
        deprecated: bool = False,
        description: Optional[str] = None,
        include_in_schema: bool = True,
        operation_id: Optional[str] = None,
        raises: Optional[List[Type[HTTPException]]] = None,
        response_description: Optional[str] = None,
        summary: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ):
        if not http_method:
            raise ImproperlyConfiguredException("An http_method kwarg is required")
        if isinstance(http_method, list):
            self.http_method: Union[List[str], str] = [v.upper() for v in http_method]
            if len(http_method) == 1:
                self.http_method = http_method[0]
        else:
            self.http_method = http_method.value if isinstance(http_method, HttpMethod) else http_method
        if status_code:
            self.status_code = status_code
        elif isinstance(self.http_method, list):
            self.status_code = HTTP_200_OK
        elif self.http_method == HttpMethod.POST:
            self.status_code = HTTP_201_CREATED
        elif self.http_method == HttpMethod.DELETE:
            self.status_code = HTTP_204_NO_CONTENT
        else:
            self.status_code = HTTP_200_OK
        super().__init__(path=path, dependencies=dependencies, guards=guards, opt=opt)
        self.after_request = after_request
        self.before_request = before_request
        self.background_tasks = background_tasks
        self.media_type = media_type
        self.response_class = response_class
        self.response_headers = response_headers
        # OpenAPI related attributes
        self.content_encoding = content_encoding
        self.content_media_type = content_media_type
        self.deprecated = deprecated
        self.description = description
        self.include_in_schema = include_in_schema
        self.operation_id = operation_id
        self.raises = raises
        self.response_description = response_description
        self.summary = summary
        self.tags = tags
        # memoized attributes, defaulted to _empty
        self.resolved_headers: Union[Dict[str, ResponseHeader], Type[_empty]] = _empty
        self.resolved_response_class: Union[Type[Response], Type[_empty]] = _empty
        self.resolved_after_request: Union[Optional[BeforeRequestHandler], Type[_empty]] = _empty
        self.resolved_before_request: Union[Optional[BeforeRequestHandler], Type[_empty]] = _empty

    def __call__(self, fn: AnyCallable) -> "HTTPRouteHandler":
        """
        Replaces a function with itself
        """
        self.fn = fn
        self.validate_handler_function()
        return self

    def ownership_layers(self) -> Generator[Union["HTTPRouteHandler", Controller, "Router"], None, None]:
        """
        Returns all the handler and then all owners up to the app level

        handler -> ... -> App
        """
        return cast(Generator[Union["HTTPRouteHandler", Controller, "Router"], None, None], super().ownership_layers())

    def resolve_response_class(self) -> Type[Response]:
        """Return the closest custom Response class in the owner graph or the default Response class"""
        if self.resolved_response_class is _empty:
            self.resolved_response_class = Response
            for layer in self.ownership_layers():
                if layer.response_class is not None:
                    self.resolved_response_class = layer.response_class
                    break
        return cast(Type[Response], self.resolved_response_class)

    def resolve_response_headers(self) -> Dict[str, ResponseHeader]:
        """
        Returns all header parameters in the scope of the handler function
        """
        if self.resolved_headers is _empty:
            headers: Dict[str, ResponseHeader] = {}
            for layer in self.ownership_layers():
                for key, value in (layer.response_headers or {}).items():
                    if key not in headers:
                        headers[key] = value
            self.resolved_headers = headers
        return cast(Dict[str, ResponseHeader], self.resolved_headers)

    def resolve_before_request(self) -> Optional[BeforeRequestHandler]:
        """
        Resolves the before_handler handler by starting from the handler and moving up.

        If a handler is found it is returned, otherwise None is set.
        This mehtod is memoized so the computation occurs only once
        """
        if self.resolved_before_request is _empty:
            for layer in self.ownership_layers():
                if layer.before_request:
                    self.resolved_before_request = layer.before_request
                    break
            if self.resolved_before_request is _empty:
                self.resolved_before_request = None
            elif ismethod(self.resolved_before_request):
                # python automatically binds class variables, which we do not want in this case.
                self.resolved_before_request = self.resolved_before_request.__func__
        return self.resolved_before_request

    def resolve_after_request(self) -> Optional[AfterRequestHandler]:
        """
        Resolves the after_request handler by starting from the handler and moving up.

        If a handler is found it is returned, otherwise None is set.
        This mehtod is memoized so the computation occurs only once
        """
        if self.resolved_after_request is _empty:
            for layer in self.ownership_layers():
                if layer.after_request:
                    self.resolved_after_request = layer.after_request  # type: ignore
                    break
            if self.resolved_after_request is _empty:
                self.resolved_after_request = None
            elif ismethod(self.resolved_after_request):
                # python automatically binds class variables, which we do not want in this case.
                self.resolved_after_request = self.resolved_after_request.__func__
        return cast(Optional[AfterRequestHandler], self.resolved_after_request)

    @property
    def http_methods(self) -> List[Method]:
        """
        Returns a list of the RouteHandler's HttpMethod members
        """
        return cast(List[Method], self.http_method if isinstance(self.http_method, list) else [self.http_method])

    def validate_handler_function(self) -> None:
        """
        Validates the route handler function once it is set by inspecting its return annotations
        """
        super().validate_handler_function()
        signature = Signature.from_callable(cast(AnyCallable, self.fn))
        return_annotation = signature.return_annotation
        if return_annotation is Signature.empty:
            raise ValidationException(
                "A return value of a route handler function should be type annotated."
                "If your function doesn't return a value or returns None, annotate it as returning None."
            )
        if isclass(return_annotation):
            with suppress(TypeError):
                if issubclass(return_annotation, Redirect) and self.status_code not in REDIRECT_STATUS_CODES:
                    raise ValidationException(
                        f"Redirect responses should have one of "
                        f"the following status codes: {', '.join([str(s) for s in REDIRECT_STATUS_CODES])}"
                    )
                if issubclass(return_annotation, File) and self.media_type in [MediaType.JSON, MediaType.HTML]:
                    self.media_type = MediaType.TEXT
        if "socket" in signature.parameters:
            raise ImproperlyConfiguredException("The 'socket' kwarg is not supported with http handlers")
        if "data" in signature.parameters and "GET" in self.http_methods:
            raise ImproperlyConfiguredException("'data' kwarg is unsupported for 'GET' request handlers")

    async def to_response(self, data: Any, plugins: List[PluginProtocol]) -> StarletteResponse:
        """
        Given a data kwarg, determine its type and return the appropriate response
        """
        if isawaitable(data):
            data = await data
        after_request = self.resolve_after_request()
        media_type = self.media_type.value if isinstance(self.media_type, Enum) else self.media_type
        headers = {k: v.value for k, v in self.resolve_response_headers().items()}
        response: StarletteResponse
        if isinstance(data, (StarletteResponse, StarliteType)):
            if isinstance(data, Redirect):
                response = RedirectResponse(headers=headers, status_code=self.status_code, url=data.path)
            elif isinstance(data, File):
                response = FileResponse(media_type=media_type, headers=headers, **data.dict())
            elif isinstance(data, Stream):
                response = StreamingResponse(
                    content=data.iterator, status_code=self.status_code, media_type=media_type, headers=headers
                )
            elif isinstance(data, Template):
                template_engine = request.app.template_engine
                if template_engine:  # noqa: SIM106
                    response = TemplateResponse(
                        context=data.context,
                        template_name=data.name,
                        template_engine=template_engine,
                        status_code=self.status_code,
                        headers=headers,
                    )
                else:
                    raise InternalServerException(detail="Template engine was not initialized in app")
            else:
                response = cast(StarletteResponse, data)
        else:
            plugin = get_plugin_for_value(value=data, plugins=plugins)
            if plugin:
                if isinstance(data, (list, tuple)):
                    data = [plugin.to_dict(datum) for datum in data]
                else:
                    data = plugin.to_dict(data)
            response_class = self.resolve_response_class()
            response = response_class(
                headers=headers,
                status_code=self.status_code,
                content=data,
                media_type=media_type,
                background=self.background_tasks,
            )
        # run the after_request hook handler
        if after_request:
            response = after_request(response)  # type: ignore
            if isawaitable(response):
                response = await response
        return response


route = HTTPRouteHandler


class get(HTTPRouteHandler):
    @validate_arguments(config={"arbitrary_types_allowed": True})
    def __init__(
        self,
        path: Union[Optional[str], Optional[List[str]]] = None,
        dependencies: Optional[Dict[str, Provide]] = None,
        guards: Optional[List[Guard]] = None,
        opt: Optional[Dict[str, Any]] = None,
        after_request: Optional[AfterRequestHandler] = None,
        before_request: Optional[BeforeRequestHandler] = None,
        media_type: Union[MediaType, str] = MediaType.JSON,
        response_class: Optional[Type[Response]] = None,
        response_headers: Optional[Dict[str, ResponseHeader]] = None,
        status_code: Optional[int] = None,
        content_encoding: Optional[str] = None,
        content_media_type: Optional[str] = None,
        deprecated: bool = False,
        description: Optional[str] = None,
        include_in_schema: bool = True,
        operation_id: Optional[str] = None,
        raises: Optional[List[Type[HTTPException]]] = None,
        response_description: Optional[str] = None,
        summary: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ):
        super().__init__(
            http_method=HttpMethod.GET,
            path=path,
            dependencies=dependencies,
            guards=guards,
            opt=opt,
            after_request=after_request,
            before_request=before_request,
            media_type=media_type,
            response_class=response_class,
            response_headers=response_headers,
            status_code=status_code,
            content_encoding=content_encoding,
            content_media_type=content_media_type,
            deprecated=deprecated,
            description=description,
            include_in_schema=include_in_schema,
            operation_id=operation_id,
            raises=raises,
            response_description=response_description,
            summary=summary,
            tags=tags,
        )


class post(HTTPRouteHandler):
    @validate_arguments(config={"arbitrary_types_allowed": True})
    def __init__(
        self,
        path: Union[Optional[str], Optional[List[str]]] = None,
        dependencies: Optional[Dict[str, Provide]] = None,
        guards: Optional[List[Guard]] = None,
        opt: Optional[Dict[str, Any]] = None,
        after_request: Optional[AfterRequestHandler] = None,
        before_request: Optional[BeforeRequestHandler] = None,
        media_type: Union[MediaType, str] = MediaType.JSON,
        response_class: Optional[Type[Response]] = None,
        response_headers: Optional[Dict[str, ResponseHeader]] = None,
        status_code: Optional[int] = None,
        content_encoding: Optional[str] = None,
        content_media_type: Optional[str] = None,
        deprecated: bool = False,
        description: Optional[str] = None,
        include_in_schema: bool = True,
        operation_id: Optional[str] = None,
        raises: Optional[List[Type[HTTPException]]] = None,
        response_description: Optional[str] = None,
        summary: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ):
        super().__init__(
            http_method=HttpMethod.POST,
            path=path,
            dependencies=dependencies,
            guards=guards,
            opt=opt,
            after_request=after_request,
            before_request=before_request,
            media_type=media_type,
            response_class=response_class,
            response_headers=response_headers,
            status_code=status_code,
            content_encoding=content_encoding,
            content_media_type=content_media_type,
            deprecated=deprecated,
            description=description,
            include_in_schema=include_in_schema,
            operation_id=operation_id,
            raises=raises,
            response_description=response_description,
            summary=summary,
            tags=tags,
        )


class put(HTTPRouteHandler):
    @validate_arguments(config={"arbitrary_types_allowed": True})
    def __init__(
        self,
        path: Union[Optional[str], Optional[List[str]]] = None,
        dependencies: Optional[Dict[str, Provide]] = None,
        guards: Optional[List[Guard]] = None,
        opt: Optional[Dict[str, Any]] = None,
        after_request: Optional[AfterRequestHandler] = None,
        before_request: Optional[BeforeRequestHandler] = None,
        media_type: Union[MediaType, str] = MediaType.JSON,
        response_class: Optional[Type[Response]] = None,
        response_headers: Optional[Dict[str, ResponseHeader]] = None,
        status_code: Optional[int] = None,
        content_encoding: Optional[str] = None,
        content_media_type: Optional[str] = None,
        deprecated: bool = False,
        description: Optional[str] = None,
        include_in_schema: bool = True,
        operation_id: Optional[str] = None,
        raises: Optional[List[Type[HTTPException]]] = None,
        response_description: Optional[str] = None,
        summary: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ):
        super().__init__(
            http_method=HttpMethod.PUT,
            path=path,
            dependencies=dependencies,
            guards=guards,
            opt=opt,
            after_request=after_request,
            before_request=before_request,
            media_type=media_type,
            response_class=response_class,
            response_headers=response_headers,
            status_code=status_code,
            content_encoding=content_encoding,
            content_media_type=content_media_type,
            deprecated=deprecated,
            description=description,
            include_in_schema=include_in_schema,
            operation_id=operation_id,
            raises=raises,
            response_description=response_description,
            summary=summary,
            tags=tags,
        )


class patch(HTTPRouteHandler):
    @validate_arguments(config={"arbitrary_types_allowed": True})
    def __init__(
        self,
        path: Union[Optional[str], Optional[List[str]]] = None,
        dependencies: Optional[Dict[str, Provide]] = None,
        guards: Optional[List[Guard]] = None,
        opt: Optional[Dict[str, Any]] = None,
        after_request: Optional[AfterRequestHandler] = None,
        before_request: Optional[BeforeRequestHandler] = None,
        media_type: Union[MediaType, str] = MediaType.JSON,
        response_class: Optional[Type[Response]] = None,
        response_headers: Optional[Dict[str, ResponseHeader]] = None,
        status_code: Optional[int] = None,
        content_encoding: Optional[str] = None,
        content_media_type: Optional[str] = None,
        deprecated: bool = False,
        description: Optional[str] = None,
        include_in_schema: bool = True,
        operation_id: Optional[str] = None,
        raises: Optional[List[Type[HTTPException]]] = None,
        response_description: Optional[str] = None,
        summary: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ):
        super().__init__(
            http_method=HttpMethod.PATCH,
            path=path,
            dependencies=dependencies,
            guards=guards,
            opt=opt,
            after_request=after_request,
            before_request=before_request,
            media_type=media_type,
            response_class=response_class,
            response_headers=response_headers,
            status_code=status_code,
            content_encoding=content_encoding,
            content_media_type=content_media_type,
            deprecated=deprecated,
            description=description,
            include_in_schema=include_in_schema,
            operation_id=operation_id,
            raises=raises,
            response_description=response_description,
            summary=summary,
            tags=tags,
        )


class delete(HTTPRouteHandler):
    @validate_arguments(config={"arbitrary_types_allowed": True})
    def __init__(
        self,
        path: Union[Optional[str], Optional[List[str]]] = None,
        dependencies: Optional[Dict[str, Provide]] = None,
        guards: Optional[List[Guard]] = None,
        opt: Optional[Dict[str, Any]] = None,
        after_request: Optional[AfterRequestHandler] = None,
        before_request: Optional[BeforeRequestHandler] = None,
        media_type: Union[MediaType, str] = MediaType.JSON,
        response_class: Optional[Type[Response]] = None,
        response_headers: Optional[Dict[str, ResponseHeader]] = None,
        status_code: Optional[int] = None,
        content_encoding: Optional[str] = None,
        content_media_type: Optional[str] = None,
        deprecated: bool = False,
        description: Optional[str] = None,
        include_in_schema: bool = True,
        operation_id: Optional[str] = None,
        raises: Optional[List[Type[HTTPException]]] = None,
        response_description: Optional[str] = None,
        summary: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ):
        super().__init__(
            http_method=HttpMethod.DELETE,
            path=path,
            dependencies=dependencies,
            guards=guards,
            opt=opt,
            after_request=after_request,
            before_request=before_request,
            media_type=media_type,
            response_class=response_class,
            response_headers=response_headers,
            status_code=status_code,
            content_encoding=content_encoding,
            content_media_type=content_media_type,
            deprecated=deprecated,
            description=description,
            include_in_schema=include_in_schema,
            operation_id=operation_id,
            raises=raises,
            response_description=response_description,
            summary=summary,
            tags=tags,
        )


class WebsocketRouteHandler(BaseRouteHandler):
    def __call__(self, fn: AsyncAnyCallable) -> "WebsocketRouteHandler":
        """
        Replaces a function with itself
        """
        self.fn = fn
        self.validate_handler_function()
        return self

    def validate_handler_function(self) -> None:
        """
        Validates the route handler function once it's set by inspecting its return annotations
        """
        super().validate_handler_function()
        signature = Signature.from_callable(cast(AnyCallable, self.fn))

        if signature.return_annotation is not None:
            raise ImproperlyConfiguredException("Websocket handler functions should return 'None'")
        if "socket" not in signature.parameters:
            raise ImproperlyConfiguredException("Websocket handlers must set a 'socket' kwarg")
        if "request" in signature.parameters:
            raise ImproperlyConfiguredException("The 'request' kwarg is not supported with websocket handlers")
        if "data" in signature.parameters:
            raise ImproperlyConfiguredException("The 'data' kwarg is not supported with websocket handlers")


websocket = WebsocketRouteHandler


class ASGIRouteHandler(BaseRouteHandler):
    def __call__(self, fn: AnyCallable) -> "ASGIRouteHandler":
        """
        Replaces a function with itself
        """
        self.fn = fn
        self.validate_handler_function()
        return self

    def validate_handler_function(self) -> None:
        """
        Validates the route handler function once it's set by inspecting its return annotations
        """
        super().validate_handler_function()
        signature = Signature.from_callable(cast(AnyCallable, self.fn))

        if signature.return_annotation is not None:
            raise ImproperlyConfiguredException("ASGI handler functions should return 'None'")
        if any(key not in signature.parameters for key in ["scope", "send", "receive"]):
            raise ImproperlyConfiguredException(
                "ASGI handler functions should define 'scope', 'send' and 'receive' arguments"
            )


asgi = ASGIRouteHandler
