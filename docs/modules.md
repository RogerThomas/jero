# API Reference

The full public surface, grouped by area. Everything here is importable from `jero`
(test helpers from `jero.testing`).

## App & wiring

::: jero.BaseApp
::: jero.BaseFactory
::: jero.WiringError

## Routing

::: jero.Resource
::: jero.Endpoint
::: jero.ResourceMeta
::: jero.EndpointMeta
::: jero.OperationMeta
::: jero.ResponseSpec
::: jero.Tag
::: jero.HTTPMethod

## Requests

::: jero.Request
::: jero.RawHeaders
::: jero.NoHeaders
::: jero.FormPart
::: jero.FilePart

## Responses

::: jero.JSONResponse
::: jero.BytesResponse
::: jero.NoContent
::: jero.Created
::: jero.Accepted
::: jero.Location
::: jero.Link

## Streaming

::: jero.StreamingResponse
::: jero.NDJSONStreamingResponse
::: jero.SSEResponse
::: jero.ServerSentEvent

## Cookies

::: jero.SetCookie

## Errors

::: jero.BaseHTTPError
::: jero.HTTPError
::: jero.DataclassHTTPError
::: jero.ParameterizedHTTPError
::: jero.StructHTTPError
::: jero.Problem
::: jero.ParameterizedProblem
::: jero.ErrorBodyAdapter
::: jero.ErrorReason
::: jero.ExceptionResponse

### Shipped errors

::: jero.AuthenticationRequiredError
::: jero.ConflictError
::: jero.ForbiddenError
::: jero.GoneError
::: jero.InternalServerError
::: jero.MalformedRequestError
::: jero.MethodNotAllowedError
::: jero.NotFoundError
::: jero.TooManyRequestsError
::: jero.UnsupportedMediaTypeError
::: jero.ValidationFailedError

## Authentication

::: jero.Auth
::: jero.CookieAuth
::: jero.HybridAuth
::: jero.BearerAuth
::: jero.BasicAuth
::: jero.SecurityScheme

## Middleware & CORS

::: jero.CORS

## Background tasks

::: jero.BackgroundTasks

## Models & codecs

::: jero.Struct
::: jero.ModelMeta
::: jero.msgspec_decoder
::: jero.msgspec_encoder

## OpenAPI

::: jero.ScalarConfig

## Testing

::: jero.testing.TestClient
::: jero.testing.TestResponse
::: jero.testing.TestCookie
::: jero.testing.TestSSEEvent
::: jero.testing.FactoryHarness
