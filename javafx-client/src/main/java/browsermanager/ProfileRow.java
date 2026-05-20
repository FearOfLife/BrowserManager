package browsermanager;

import java.util.Map;

import javafx.beans.property.BooleanProperty;
import javafx.beans.property.SimpleBooleanProperty;
import javafx.beans.property.SimpleStringProperty;
import javafx.beans.property.StringProperty;

public final class ProfileRow {
    private final BooleanProperty selected = new SimpleBooleanProperty(false);
    private final StringProperty id = new SimpleStringProperty("");
    private final StringProperty os = new SimpleStringProperty("WIN");
    private final StringProperty name = new SimpleStringProperty("");
    private final StringProperty folder = new SimpleStringProperty("BrowserManager");
    private final StringProperty status = new SimpleStringProperty("");
    private final StringProperty tags = new SimpleStringProperty("");
    private final StringProperty proxy = new SimpleStringProperty("");
    private final StringProperty proxyCheckLabel = new SimpleStringProperty("");
    private final StringProperty proxyCheckState = new SimpleStringProperty("");
    private final StringProperty localPort = new SimpleStringProperty("");
    private Map<String, Object> raw;

    public static ProfileRow fromMap(Map<String, Object> map) {
        ProfileRow row = new ProfileRow();
        row.updateFrom(map);
        return row;
    }

    public void updateFrom(Map<String, Object> map) {
        raw = map;
        id.set(Json.string(map.get("id")));
        name.set(Json.string(map.get("name")));
        folder.set(Json.string(map.get("folder")).isBlank() ? "BrowserManager" : Json.string(map.get("folder")));
        os.set(osKind(Json.asMap(map.get("fingerprint"))));
        status.set(Json.string(map.get("status")));
        tags.set(Json.string(map.get("notes")).isBlank() ? "теги" : Json.string(map.get("notes")));
        proxy.set(Json.string(map.get("proxy_label")));
        proxyCheckLabel.set(Json.string(map.get("proxy_check_label")));
        proxyCheckState.set(Json.string(map.get("proxy_check_state")));
        localPort.set(Json.string(map.get("local_port")).isBlank() ? "авто" : Json.string(map.get("local_port")));
    }

    private String osKind(Map<String, Object> fingerprint) {
        String platform = Json.string(fingerprint.get("platform")).toLowerCase();
        String userAgent = Json.string(fingerprint.get("user_agent")).toLowerCase();
        if (platform.contains("mac") || userAgent.contains("macintosh")) {
            return "mac";
        }
        if (platform.contains("linux") || userAgent.contains("linux")) {
            return "linux";
        }
        return "windows";
    }

    public String rawString(String key) {
        return Json.string(raw == null ? null : raw.get(key));
    }

    public Map<String, Object> raw() {
        return raw;
    }

    public BooleanProperty selectedProperty() {
        return selected;
    }

    public boolean isSelected() {
        return selected.get();
    }

    public void setSelected(boolean value) {
        selected.set(value);
    }

    public StringProperty idProperty() {
        return id;
    }

    public String getId() {
        return id.get();
    }

    public StringProperty osProperty() {
        return os;
    }

    public StringProperty nameProperty() {
        return name;
    }

    public String getName() {
        return name.get();
    }

    public StringProperty folderProperty() {
        return folder;
    }

    public StringProperty statusProperty() {
        return status;
    }

    public StringProperty tagsProperty() {
        return tags;
    }

    public StringProperty proxyProperty() {
        return proxy;
    }

    public StringProperty proxyCheckLabelProperty() {
        return proxyCheckLabel;
    }

    public StringProperty proxyCheckStateProperty() {
        return proxyCheckState;
    }

    public StringProperty localPortProperty() {
        return localPort;
    }
}
