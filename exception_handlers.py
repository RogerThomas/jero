from dataclasses import dataclass

from msgspec import Struct


class MyException(Exception):
    pass


class AnalyticsService:
    def log_analytics_event(self, event_name: str) -> None:
        # Placeholder for logging an analytics event
        print(f"Logging event: {event_name}")


class CustomReturn(Struct):
    error_message: str


class BaseExceptionHandler[E: Exception, T: Struct | None = None](abc):
    _analytics_service: AnalyticsService

    def handle_exception(self, exception: E) -> T: ...


class MyExceptionHandler(BaseExceptionHandler[MyException, CustomReturn]):
    def __init__(self, analytics_service: AnalyticsService):
        self._analytics_service = analytics_service

    def handle_exception(self, exception: MyException) -> CustomReturn:
        # Log the exception to the analytics service
        self._analytics_service.log_analytics_event("MyException occurred")
        # Return a custom response
        return CustomReturn(error_message=str(exception))


class MyResource:
    def create(self):
        raise MyException()


class App:
    def _wire(self):
        self._add_exception_handler(MyExceptionHandler(self._analytics_service))
        self._include_resource(MyResource())
