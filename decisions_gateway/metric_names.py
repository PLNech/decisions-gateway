"""Every exported metric name and closed label enum lives here, so a rename is a one-file change."""

PREFIX = "decisions_gateway_"

REQUESTS = PREFIX + "requests_total"
DURATION = PREFIX + "request_duration_seconds"
QUEUE = PREFIX + "queue_time_seconds"
INFERENCE = PREFIX + "inference_time_seconds"
OVERHEAD = PREFIX + "overhead_seconds"
IN_FLIGHT = PREFIX + "in_flight_requests"
REJECTIONS = PREFIX + "rejections_total"
BACKEND_ERRORS = PREFIX + "backend_errors_total"
BACKEND_UP = PREFIX + "backend_up"
REQUEST_BYTES = PREFIX + "request_bytes"
QUESTIONS_PER_REQUEST = PREFIX + "questions_per_request"
QUESTIONS = PREFIX + "questions_total"
BUILD_INFO = PREFIX + "build_info"

HELP = {
    REQUESTS: ("counter", "Decision requests by backend, model, client and outcome."),
    DURATION: ("histogram", "Gateway wall time per request."),
    QUEUE: ("histogram", "Backend scheduler queue time, from its Server-Timing header."),
    INFERENCE: ("histogram", "Backend inference time, from its Server-Timing header."),
    OVERHEAD: ("histogram", "Gateway wall time minus the backend's own total."),
    IN_FLIGHT: ("gauge", "Requests holding a backend slot."),
    REJECTIONS: ("counter", "Requests the gateway refused, by reason."),
    BACKEND_ERRORS: ("counter", "Failed backend exchanges, by reason."),
    BACKEND_UP: ("gauge", "1 if the last exchange or probe reached the backend."),
    REQUEST_BYTES: ("histogram", "Request body size."),
    QUESTIONS_PER_REQUEST: ("histogram", "Questions per request."),
    QUESTIONS: ("counter", "Questions received, by question type."),
    BUILD_INFO: ("gauge", "Always 1; the version label names the running build."),
}

# Closed enums: label values never come from request content.
OUTCOMES = ("ok", "client_error", "backend_error", "rejected")
REJECTION_REASONS = (
    "auth",
    "host",
    "content_type",
    "length_required",
    "payload_too_large",
    "too_many_questions",
    "concurrency_cap",
    "overloaded",
    "backend_down",
    "deadline",
)
BACKEND_ERROR_REASONS = ("connect", "timeout", "protocol")
