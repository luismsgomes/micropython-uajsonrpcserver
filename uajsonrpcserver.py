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

__author__ = "Luís Gomes"
__version__ = "0.2.0"


import json
import uasyncio


JSONRPC_VERSION = "2.0"


class RequestError(Exception):
    "Umbrella class for all exceptions raised by UAJSONRPCServer.handle_request()"

    def __init__(self, request_id=None, data=None):
        self.request_id = request_id
        self.data = data

    def get_response(self):
        "returns a JSON-RPC formatted response containing error data"
        error = {
            "code": self.code,
            "message": self.message,
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
    "Error raised when request parameters do not conform to the method signature"
    code = -32602
    message = "Invalid params"


class ServerError(RequestError):
    "Error raised when the method raises an exception"
    code = -32000
    message = "Server error"


class UAJSONRPCServer:
    """
    Asynchronous JSON-RPC server implementation (based on uasyncio)

    Example messages:
    --> {"jsonrpc": "2.0", "method": "subtract", "params": \
            {"minuend": 42, "subtrahend": 23}, "id": 3}
    <-- {"jsonrpc": "2.0", "result": 19, "id": 3}
    """

    def __init__(self, host="0.0.0.0", port=10000):
        self.host = host
        self.port = port
        self.methods = dict()
        self.server = None

    def register(self, name, method, param_names, is_coroutine=False):
        "register a method to become available to JSON-RPC clients"
        self.methods[name] = (method, param_names, is_coroutine)

    async def start(self):
        "starts the server as an asynchronous task (coroutine)"
        if self.server is None:
            self.server = await uasyncio.start_server(
                self.handle_connection,
                self.host,
                self.port,
            )
            print(f"Started server; listening on {self.host}:{self.port}")
        else:
            print("Called start() on server already started")

    async def stop(self):
        "stops the server"
        print("Stopping the server...")
        self.server.close()
        await self.server.wait_closed()
        self.server = None
        print("Server stoped")

    async def handle_connection(self, reader, writer):
        "handles a client connect calling self.handle_request() for each request"
        peername = writer.get_extra_info("peername")
        print(f"Accepted client connection from {peername}")
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

    async def handle_request(self, request_str):
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
            raise MethodNotFound(request_id)
        params = request_dict.get("params", None)
        method, param_names, is_coroutine = self.methods[method_name]
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
                param_names_str = ", ".join(param_names)
                raise InvalidParams(
                    data=f"Method {method_name} param names are: {param_names_str}"
                )
            positional_params = []
            named_params = params
        else:
            raise InvalidRequest(data="Params must be an array or object")
        try:
            if is_coroutine:
                result = await method(*positional_params, **named_params)
            else:
                result = method(*positional_params, **named_params)
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
