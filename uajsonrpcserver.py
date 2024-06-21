"""
uajsonrpcserver, asyncronous JSON-RPC server for MicroPython

Copyright 2024, Luís Gomes <luismsgomes@gmail.com>

This program is free software: you can redistribute it
and/or modify it under the terms of the GNU General Public License
as published by the Free Software Foundation, either version 3 of
the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

import json

try:
    from typing import Callable, Coroutine, Dict, List, Optional, Tuple
except ImportError:  # no typing module on MicroPython yet
    async def _f(): pass
    Coroutine = type(_f())  # type: ignore

try:
    import uasyncio as asyncio  # type: ignore[import-not-found]
except ImportError:
    import asyncio


JSONRPC_VERSION = "2.0"


class RequestError(Exception):
    "Umbrella class for all exceptions raised by UAJSONRPCServer.handle_request()"

    def __init__(self, request_id=None, data=None):
        self.request_id = request_id
        self.data = data

    def get_response(self) -> str:
        "returns a JSON-RPC formatted response containing error data"
        error = {
            "code": self.code,  # type: ignore[attr-defined]
            "message": self.message,  # type: ignore[attr-defined]
        }
        if self.data is not None:
            error["data"] = self.data
        response = {
            "jsonrpc": JSONRPC_VERSION,
            "error": error,
        }
        if self.request_id is not None:
            response["id"] = self.request_id
        return json.dumps(response)


class RequestParseError(RequestError):
    "Error raised when request is not valid JSON"
    code = -32700
    message = "Parse error"


class InvalidRequest(RequestError):
    "Error raised when request is valid JSON but not a valid JSON object"
    code = -32600
    message = "Invalid request"


class MethodNotFound(RequestError):
    "Error raised when requested method does not exist"
    code = -32601
    message = "Method not found"


class InvalidParams(RequestError):
    "Error raised when the parameters in the request do not conform to the method signature"
    code = -32602
    message = "Invalid params"


class ServerError(RequestError):
    "Error raised when the method raises an exception"
    code = -32000
    message = "Server error"


def get_peername(reader: asyncio.StreamReader) -> str | None:
    peername = None
    if hasattr(reader, "get_extra_info"):
        peername = reader.get_extra_info("peername")  # type: ignore[attr-defined]
    elif (
        hasattr(reader, "_transport")
        and hasattr(reader._transport, "_extra")  # type: ignore[attr-defined]
        and isinstance(reader._transport._extra, dict)  # type: ignore[attr-defined]
        and "peername" in reader._transport._extra  # type: ignore[attr-defined]
    ):
        peername = reader._transport._extra["peername"]  # type: ignore[attr-defined]
    return peername


class UAJSONRPCServer:
    """
    Asynchronous JSON-RPC server implementation (based on uasyncio)

    Example messages:
    --> {"jsonrpc": "2.0", "method": "subtract", "params": {"minuend": 42, "subtrahend": 23}, "id": 3}
    <-- {"jsonrpc": "2.0", "result": 19, "id": 3}
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 10000):
        self.host = host
        self.port = port
        self.methods: Dict[str, Tuple[Callable, List[str]]] = dict()
        self.task: Optional[asyncio.Task] = None

    def register(self, name: str, method: Callable, param_names: List[str]) -> None:
        "register a method to become available to JSON-RPC clients"
        self.methods[name] = (method, param_names)

    def start(self) -> None:
        "starts the server as an asynchronous task (coroutine)"
        print(f"Listening on {self.host}:{self.port}")
        self.task = asyncio.create_task(
            asyncio.start_server(self.handle_connection, self.host, self.port)
        )

    async def stop(self) -> None:
        task: asyncio.Task = self.task
        delattr(self, "task")
        task.cancel()
        await task

    async def handle_connection(self, reader, writer) -> None:
        "handles a client connect calling self.handle_request() for each request"
        print(f"Accepted client connection from {get_peername(reader)}")
        try:
            fatal_error = False
            while not fatal_error:
                request_str = (await reader.readline()).decode()
                if not request_str:
                    break
                request_str = request_str.strip()
                response_str = None
                print(f"<-- {request_str}")
                try:
                    response_str = await self.handle_request(request_str)
                except RequestError as exception:
                    response_str = exception.get_response()
                    if isinstance(exception, (RequestParseError, InvalidRequest)):
                        fatal_error = True  # stop handling requests on this socket
                if response_str:
                    writer.write((response_str + "\n").encode("utf-8"))
                    await writer.drain()
                    print(f"--> {response_str}")
        except Exception as e:
            print(f"Exception: {e!r}")
        finally:
            reader.close()
            writer.close()
            print("Connection closed")

    async def handle_request(self, request_str: str) -> str | None:
        "handle a single request"
        # see https://www.jsonrpc.org/specification#request_object
        try:
            request_dict = json.loads(request_str)
        except ValueError as e:
            raise RequestParseError() from e
        if not isinstance(request_dict, dict):
            raise InvalidRequest()
        jsonrpc_version = request_dict.get("jsonrpc", None)
        if jsonrpc_version != JSONRPC_VERSION:
            raise InvalidRequest()
        request_id = request_dict.get("id", None)
        method_name = request_dict.get("method", None)
        if method_name not in self.methods:
            raise MethodNotFound(request_id=request_id)
        params = request_dict.get("params", None)
        method, param_names = self.methods[method_name]
        num_params = len(param_names)
        if (
            params is None
            and num_params
            or isinstance(params, (list, dict))
            and len(params) != num_params
        ):
            raise InvalidParams(data=f"Method {method_name} takes {num_params} params")
        if isinstance(params, list):
            positional_params = params
            named_params = {}
        elif isinstance(params, dict):
            if set(param_names) != set(params.keys()):
                raise InvalidParams(
                    data=f"Method {method_name} param names are: {', '.join(param_names)}"
                )
            positional_params = []
            named_params = params
        else:
            raise InvalidRequest(data="Params must be an array or object")
        try:
            result = method(*positional_params, **named_params)
            if isinstance(result, Coroutine):
                result = await result
        except Exception as e:
            raise ServerError(request_id, repr(e)) from e
        if request_id is None:
            return None  # request was notification; no response needed
        response_obj = {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "result": result,
        }
        return json.dumps(response_obj)
