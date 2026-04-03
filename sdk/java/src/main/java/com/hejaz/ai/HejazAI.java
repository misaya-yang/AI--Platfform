package com.hejaz.ai;

import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.net.http.HttpClient;
import java.time.Duration;

/**
 * Main entry point for the Hejaz AI Java SDK.
 *
 * <p>Create a single instance per application and access sub-clients for
 * each capability. Uses the JDK's {@link java.net.http.HttpClient} for
 * HTTP and SSE streaming — no additional HTTP library required.
 *
 * <h2>Quick start</h2>
 * <pre>{@code
 * try (var ai = new HejazAI("gw_xxx")) {
 *     // Non-streaming
 *     ChatResponse resp = ai.chat().send("What is Zakat?").join();
 *     System.out.println(resp.content());
 *
 *     // Streaming
 *     ai.chat().stream("Explain fasting", new StreamHandler() {
 *         @Override public void onText(String text) { System.out.print(text); }
 *         @Override public void onDone(ChatResponse r) { System.out.println("\nDone!"); }
 *     });
 * }
 * }</pre>
 *
 * <h2>Multi-tenant</h2>
 * <pre>{@code
 * var ai = new HejazAI("gw_xxx", "https://api.hejaz.com", "wahda");
 * }</pre>
 *
 * <p>Implements {@link AutoCloseable} for use in try-with-resources blocks.
 */
public class HejazAI implements AutoCloseable {

    /** SDK version — kept in sync with pom.xml. */
    public static final String VERSION = "1.0.0";

    private final String apiKey;
    private final String baseUrl;
    private final String tenantId;
    private final HttpClient httpClient;
    private final ObjectMapper objectMapper;
    private final ChatClient chat;

    // =========================================================================
    // Constructors
    // =========================================================================

    /**
     * Create a client with the default base URL ({@code https://api.hejaz.com})
     * and no tenant scoping.
     *
     * @param apiKey API key issued by the AI Gateway (e.g. {@code "gw_xxx"}).
     */
    public HejazAI(String apiKey) {
        this(apiKey, "https://api.hejaz.com", "");
    }

    /**
     * Create a client with custom base URL and tenant.
     *
     * @param apiKey   API key issued by the AI Gateway.
     * @param baseUrl  Gateway base URL (no trailing slash).
     * @param tenantId Multi-tenant identifier (empty string for default tenant).
     */
    public HejazAI(String apiKey, String baseUrl, String tenantId) {
        this(apiKey, baseUrl, tenantId, Duration.ofSeconds(30));
    }

    /**
     * Create a client with full configuration.
     *
     * @param apiKey   API key issued by the AI Gateway.
     * @param baseUrl  Gateway base URL (no trailing slash).
     * @param tenantId Multi-tenant identifier (empty string for default tenant).
     * @param timeout  Default HTTP request timeout.
     */
    public HejazAI(String apiKey, String baseUrl, String tenantId, Duration timeout) {
        if (apiKey == null || apiKey.isBlank()) {
            throw new IllegalArgumentException("apiKey must not be null or blank");
        }
        if (baseUrl == null || baseUrl.isBlank()) {
            throw new IllegalArgumentException("baseUrl must not be null or blank");
        }

        this.apiKey = apiKey;
        this.baseUrl = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        this.tenantId = tenantId != null ? tenantId : "";

        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .followRedirects(HttpClient.Redirect.NORMAL)
                .build();

        this.objectMapper = new ObjectMapper()
                .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false);

        this.chat = new ChatClient(httpClient, this.baseUrl, this.apiKey,
                this.tenantId, this.objectMapper, timeout);
    }

    // =========================================================================
    // Sub-client accessors
    // =========================================================================

    /**
     * Access the chat client for non-streaming and streaming completions.
     *
     * @return The {@link ChatClient} instance.
     */
    public ChatClient chat() {
        return chat;
    }

    // =========================================================================
    // Configuration accessors
    // =========================================================================

    /** The API key this client authenticates with. */
    public String getApiKey() {
        return apiKey;
    }

    /** The gateway base URL this client connects to. */
    public String getBaseUrl() {
        return baseUrl;
    }

    /** The tenant ID used for multi-tenant scoping. */
    public String getTenantId() {
        return tenantId;
    }

    // =========================================================================
    // Lifecycle
    // =========================================================================

    /**
     * Release all underlying HTTP connections.
     *
     * <p>Safe to call multiple times. After closing, any in-flight requests
     * may fail with exceptions.
     */
    @Override
    public void close() {
        // JDK HttpClient does not require explicit closing in Java 17,
        // but we provide this for forward compatibility and resource hygiene.
        // In Java 21+ HttpClient implements AutoCloseable.
    }

    @Override
    public String toString() {
        return "HejazAI(baseUrl=" + baseUrl
                + ", tenantId=" + (tenantId.isEmpty() ? "<default>" : tenantId)
                + ", version=" + VERSION + ")";
    }
}
