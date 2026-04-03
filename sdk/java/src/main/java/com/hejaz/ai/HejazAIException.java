package com.hejaz.ai;

/**
 * Base exception for all Hejaz AI SDK errors.
 *
 * <p>Carries structured metadata (HTTP status code, message, request ID) so
 * callers can log, retry, or surface errors without parsing strings.
 */
public class HejazAIException extends RuntimeException {

    private final Integer statusCode;
    private final String requestId;

    public HejazAIException(String message) {
        this(message, null, null, null);
    }

    public HejazAIException(String message, Throwable cause) {
        this(message, null, null, cause);
    }

    public HejazAIException(String message, Integer statusCode, String requestId) {
        this(message, statusCode, requestId, null);
    }

    public HejazAIException(String message, Integer statusCode, String requestId, Throwable cause) {
        super(message, cause);
        this.statusCode = statusCode;
        this.requestId = requestId;
    }

    /** HTTP status code from the gateway, or {@code null} for client-side errors. */
    public Integer getStatusCode() {
        return statusCode;
    }

    /** Gateway-assigned request ID for tracing, or {@code null} if unavailable. */
    public String getRequestId() {
        return requestId;
    }

    @Override
    public String toString() {
        var sb = new StringBuilder(getClass().getSimpleName());
        sb.append("(message=").append(getMessage());
        if (statusCode != null) sb.append(", statusCode=").append(statusCode);
        if (requestId != null) sb.append(", requestId=").append(requestId);
        sb.append(")");
        return sb.toString();
    }

    // -------------------------------------------------------------------------
    // Factory — maps HTTP status codes to typed exceptions
    // -------------------------------------------------------------------------

    /**
     * Create the appropriate exception subclass for a given HTTP status code.
     */
    public static HejazAIException fromStatus(int statusCode, String message, String requestId) {
        return switch (statusCode) {
            case 401, 403 -> new AuthException(message, statusCode, requestId);
            case 429 -> new RateLimitException(message, statusCode, requestId, null);
            case 404 -> new HejazAIException("Not found: " + message, statusCode, requestId);
            case 422 -> new HejazAIException("Validation error: " + message, statusCode, requestId);
            case 500, 502, 503, 504 -> new HejazAIException("Server error: " + message, statusCode, requestId);
            default -> new HejazAIException(message, statusCode, requestId);
        };
    }
}
