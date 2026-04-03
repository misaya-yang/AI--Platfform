package com.hejaz.ai;

/**
 * SSE connection or parsing failure.
 *
 * <p>Thrown when the streaming connection drops unexpectedly, the server
 * sends malformed SSE data, or a JSON decode error occurs mid-stream.
 */
public class StreamException extends HejazAIException {

    public StreamException(String message) {
        super(message);
    }

    public StreamException(String message, Throwable cause) {
        super(message, cause);
    }
}
