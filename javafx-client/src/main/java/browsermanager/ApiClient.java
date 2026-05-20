package browsermanager;

import java.io.IOException;
import java.net.ConnectException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class ApiClient {
    private final URI baseUri;
    private final HttpClient http;

    public ApiClient(String baseUrl) {
        this.baseUri = URI.create(baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl);
        this.http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
    }

    public boolean isHealthy() {
        try {
            Map<String, Object> payload = get("/api/health");
            return Json.bool(payload.get("ok"));
        } catch (Exception exc) {
            return false;
        }
    }

    public List<ProfileRow> profiles(String folder) throws IOException, InterruptedException {
        String query = folder == null || folder.isBlank()
                ? ""
                : "?folder=" + URLEncoder.encode(folder, StandardCharsets.UTF_8);
        Map<String, Object> payload = get("/api/profiles" + query);
        List<ProfileRow> rows = new ArrayList<>();
        for (Object item : Json.asList(payload.get("profiles"))) {
            rows.add(ProfileRow.fromMap(Json.asMap(item)));
        }
        return rows;
    }

    public List<String> folders() throws IOException, InterruptedException {
        Map<String, Object> payload = get("/api/folders");
        List<String> rows = new ArrayList<>();
        for (Object item : Json.asList(payload.get("folders"))) {
            rows.add(Json.string(item));
        }
        return rows;
    }

    public List<String> proxies() throws IOException, InterruptedException {
        Map<String, Object> payload = get("/api/proxies");
        List<String> rows = new ArrayList<>();
        for (Object item : Json.asList(payload.get("proxies"))) {
            rows.add(Json.string(item));
        }
        return rows;
    }

    public List<String> logs(long since) throws IOException, InterruptedException {
        Map<String, Object> payload = get("/api/logs?since=" + since);
        List<String> rows = new ArrayList<>();
        for (Object item : Json.asList(payload.get("logs"))) {
            Map<String, Object> log = Json.asMap(item);
            rows.add(Json.string(log.get("index")) + "\t" + Json.string(log.get("message")));
        }
        return rows;
    }

    public void createProfile(String folder) throws IOException, InterruptedException {
        post("/api/profiles", Map.of("folder", folder == null ? "" : folder));
    }

    public void createFolder(String name) throws IOException, InterruptedException {
        post("/api/folders", Map.of("name", name));
    }

    public void updateProfile(Map<String, Object> profile) throws IOException, InterruptedException {
        post("/api/profiles/update", profile);
    }

    public void start(Collection<String> ids) throws IOException, InterruptedException {
        post("/api/profiles/start", idsPayload(ids));
    }

    public void stop(Collection<String> ids) throws IOException, InterruptedException {
        post("/api/profiles/stop", idsPayload(ids));
    }

    public void duplicate(Collection<String> ids) throws IOException, InterruptedException {
        post("/api/profiles/duplicate", idsPayload(ids));
    }

    public void delete(Collection<String> ids) throws IOException, InterruptedException {
        post("/api/profiles/delete", idsPayload(ids));
    }

    public void randomFingerprint(Collection<String> ids) throws IOException, InterruptedException {
        post("/api/fingerprint/randomize", idsPayload(ids));
    }

    public void randomProxy(Collection<String> ids) throws IOException, InterruptedException {
        post("/api/proxy/random-assign", idsPayload(ids));
    }

    public void checkProxy(Collection<String> ids) throws IOException, InterruptedException {
        post("/api/proxy/check", idsPayload(ids));
    }

    public void saveProxies(String text) throws IOException, InterruptedException {
        List<String> lines = text.lines().map(String::trim).filter(line -> !line.isBlank()).toList();
        post("/api/proxies", Map.of("proxies", lines));
    }

    public void shutdown() {
        try {
            post("/api/shutdown", Map.of());
        } catch (Exception ignored) {
        }
    }

    private Map<String, Object> idsPayload(Collection<String> ids) {
        return new LinkedHashMap<>(Map.of("ids", new ArrayList<>(ids)));
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> get(String path) throws IOException, InterruptedException {
        HttpRequest request = HttpRequest.newBuilder(baseUri.resolve(path))
                .timeout(Duration.ofSeconds(10))
                .GET()
                .build();
        HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        return check(response);
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> post(String path, Map<String, Object> body) throws IOException, InterruptedException {
        HttpRequest request = HttpRequest.newBuilder(baseUri.resolve(path))
                .timeout(Duration.ofSeconds(20))
                .header("Content-Type", "application/json; charset=utf-8")
                .POST(HttpRequest.BodyPublishers.ofString(Json.stringify(body), StandardCharsets.UTF_8))
                .build();
        HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        return check(response);
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> check(HttpResponse<String> response) throws ConnectException {
        Object parsed = Json.parse(response.body());
        Map<String, Object> map = Json.asMap(parsed);
        if (response.statusCode() >= 400 || Boolean.FALSE.equals(map.get("ok"))) {
            throw new ConnectException(Json.string(map.getOrDefault("error", "HTTP " + response.statusCode())));
        }
        return map;
    }
}
