package browsermanager;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import javafx.animation.KeyFrame;
import javafx.animation.Timeline;
import javafx.application.Application;
import javafx.application.Platform;
import javafx.collections.FXCollections;
import javafx.collections.ObservableList;
import javafx.geometry.Insets;
import javafx.geometry.Pos;
import javafx.scene.Scene;
import javafx.scene.control.Alert;
import javafx.scene.control.Button;
import javafx.scene.control.ButtonBar;
import javafx.scene.control.ButtonType;
import javafx.scene.control.CheckBox;
import javafx.scene.control.Dialog;
import javafx.scene.control.Label;
import javafx.scene.control.ListView;
import javafx.scene.control.SelectionMode;
import javafx.scene.control.TableCell;
import javafx.scene.control.TableColumn;
import javafx.scene.control.TableRow;
import javafx.scene.control.TableView;
import javafx.scene.control.TextArea;
import javafx.scene.control.TextField;
import javafx.scene.control.TextInputDialog;
import javafx.scene.control.cell.CheckBoxTableCell;
import javafx.scene.layout.BorderPane;
import javafx.scene.layout.GridPane;
import javafx.scene.layout.HBox;
import javafx.scene.layout.Pane;
import javafx.scene.layout.Priority;
import javafx.scene.layout.Region;
import javafx.scene.layout.StackPane;
import javafx.scene.layout.VBox;
import javafx.scene.shape.Rectangle;
import javafx.stage.Stage;
import javafx.util.Duration;

public final class BrowserManagerFx extends Application {
    private static final int API_PORT = Integer.getInteger("browser.manager.port", 8765);
    private static final String DEFAULT_FOLDER = "BrowserManager";
    private static final String ALL_FOLDERS = "Все профили";

    private final ObservableList<ProfileRow> profiles = FXCollections.observableArrayList();
    private final ObservableList<String> folders = FXCollections.observableArrayList();
    private final ExecutorService executor = Executors.newCachedThreadPool();
    private final ApiClient api = new ApiClient("http://127.0.0.1:" + API_PORT);

    private StackPane shell;
    private TableView<ProfileRow> table;
    private ListView<String> folderList;
    private CheckBox selectAllHeader;
    private Label titleLabel;
    private Label selectedCountLabel;
    private Label statusLabel;
    private String selectedFolder = DEFAULT_FOLDER;
    private boolean updatingSelectionHeader;
    private Timeline poller;
    private Process backendProcess;

    @Override
    public void start(Stage stage) {
        startBackendIfNeeded();

        BorderPane root = new BorderPane();
        root.getStyleClass().add("root-pane");
        root.setLeft(buildSidebar());

        BorderPane content = new BorderPane();
        content.getStyleClass().add("content-pane");
        content.setTop(buildTopBar());
        content.setCenter(buildTable());
        content.setBottom(buildBottom());
        root.setCenter(content);
        shell = new StackPane(root);
        shell.getStyleClass().add("shell");

        Scene scene = new Scene(shell, 1280, 690);
        addStylesheet(scene);
        stage.setTitle("BrowserManager JavaFX");
        stage.setScene(scene);
        stage.setMinWidth(1040);
        stage.setMinHeight(560);
        stage.setOnCloseRequest(_event -> {
            if (poller != null) {
                poller.stop();
            }
            if (backendProcess != null && backendProcess.isAlive()) {
                api.shutdown();
            }
            executor.shutdownNow();
        });
        stage.show();

        refreshFolders();
        refreshProfiles();
        startPolling();
    }

    private HBox buildTopBar() {
        titleLabel = new Label(folderTitle());
        titleLabel.getStyleClass().add("app-title");
        Label subtitle = new Label("профили браузеров");
        subtitle.getStyleClass().add("subtitle");

        HBox spacer = new HBox();
        HBox.setHgrow(spacer, Priority.ALWAYS);

        Button duplicate = button("Дублировать", () -> runAction("Дублирование", () -> api.duplicate(selectedIds())));
        Button delete = button("Удалить", () -> runAction("Удаление", () -> api.delete(selectedIds())));
        Button proxyPool = button("Прокси пул", this::showProxyPoolDialog);
        Button create = button("Создать профиль", this::showNewProfileDialog);
        create.getStyleClass().add("accent-button");

        HBox top = new HBox(10, titleLabel, subtitle, spacer, duplicate, delete, proxyPool, create);
        top.setAlignment(Pos.CENTER_LEFT);
        top.setPadding(new Insets(16, 16, 8, 16));
        top.getStyleClass().add("top-bar");
        return top;
    }

    private VBox buildSidebar() {
        Label title = new Label("Папки");
        title.getStyleClass().add("sidebar-title");

        Button addFolder = button("Создать новую папку", this::showCreateFolderDialog);
        addFolder.getStyleClass().add("folder-add-button");

        Region divider = new Region();
        divider.getStyleClass().add("folder-divider");

        folderList = new ListView<>(folders);
        folderList.getStyleClass().add("folder-list");
        folderList.getSelectionModel().selectedItemProperty().addListener((_obs, _old, value) -> {
            if (value == null || value.equals(selectedFolder)) {
                return;
            }
            selectedFolder = value;
            updateFolderTitle();
            refreshProfiles();
        });

        VBox sidebar = new VBox(10, title, addFolder, divider, folderList);
        sidebar.getStyleClass().add("sidebar");
        VBox.setVgrow(folderList, Priority.ALWAYS);
        return sidebar;
    }

    private TableView<ProfileRow> buildTable() {
        table = new TableView<>(profiles);
        table.setEditable(true);
        table.getSelectionModel().setSelectionMode(SelectionMode.MULTIPLE);
        table.getStyleClass().add("profile-table");

        TableColumn<ProfileRow, Boolean> selected = new TableColumn<>("");
        selected.setCellValueFactory(data -> data.getValue().selectedProperty());
        selected.setCellFactory(CheckBoxTableCell.forTableColumn(selected));
        selectAllHeader = new CheckBox();
        selectAllHeader.getStyleClass().add("table-select-all");
        selectAllHeader.selectedProperty().addListener((_obs, _old, value) -> {
            if (updatingSelectionHeader) {
                return;
            }
            profiles.forEach(row -> row.setSelected(value));
            updateSelectionCount();
        });
        selected.setGraphic(selectAllHeader);
        selected.setPrefWidth(58);
        selected.setMinWidth(58);
        selected.setMaxWidth(72);
        selected.getStyleClass().add("center-column");

        TableColumn<ProfileRow, String> os = osColumn("ОС", 64);
        os.getStyleClass().add("os-column");
        TableColumn<ProfileRow, String> name = textColumn("Название", ProfileRow::nameProperty, 260, false);
        TableColumn<ProfileRow, String> status = statusColumn("Статус", ProfileRow::statusProperty, 130);
        TableColumn<ProfileRow, String> tags = pillColumn("Теги", ProfileRow::tagsProperty, 130, "tag-pill");
        TableColumn<ProfileRow, String> proxy = proxyColumn("Прокси", 420);
        TableColumn<ProfileRow, String> localPort = pillColumn("Лок. порт", ProfileRow::localPortProperty, 120, "port-pill");

        table.getColumns().clear();
        table.getColumns().add(selected);
        table.getColumns().add(os);
        table.getColumns().add(name);
        table.getColumns().add(status);
        table.getColumns().add(tags);
        table.getColumns().add(proxy);
        table.getColumns().add(localPort);
        table.setColumnResizePolicy(TableView.CONSTRAINED_RESIZE_POLICY);
        table.setRowFactory(_table -> {
            TableRow<ProfileRow> row = new TableRow<>();
            row.setOnMouseClicked(event -> {
                if (event.getClickCount() == 2 && !row.isEmpty()) {
                    showProfileDialog(row.getItem());
                }
            });
            return row;
        });
        return table;
    }

    private VBox buildBottom() {
        selectedCountLabel = new Label("Выбрано: 0");
        selectedCountLabel.getStyleClass().add("selected-count");

        Button start = button("ЗАПУСК", () -> runAction("Запуск", () -> api.start(selectedIds())));
        start.getStyleClass().add("start-button");
        Button stop = button("СТОП", () -> runAction("Стоп", () -> api.stop(selectedIds())));
        stop.getStyleClass().add("stop-button");
        Button fingerprint = button("Fingerprint", () -> runAction("Fingerprint", () -> api.randomFingerprint(selectedIds())));
        Button cookies = button("Обновить", this::refreshProfiles);
        Button randomProxy = button("Случайный прокси", () -> runAction("Proxy", () -> api.randomProxy(selectedIds())));
        Button settings = button("Настройки", () -> {
            ProfileRow row = focusedOrFirstSelected();
            if (row != null) {
                showProfileDialog(row);
            }
        });

        HBox actions = new HBox(10, selectedCountLabel, start, stop, fingerprint, randomProxy, cookies, settings);
        actions.setAlignment(Pos.CENTER_LEFT);
        actions.setPadding(new Insets(10, 16, 10, 16));
        actions.getStyleClass().add("action-bar");

        statusLabel = new Label("Готов");
        statusLabel.getStyleClass().add("status-label");

        VBox bottom = new VBox(8, actions, statusLabel);
        bottom.setPadding(new Insets(8, 16, 8, 16));
        return bottom;
    }

    private TableColumn<ProfileRow, String> textColumn(
            String title,
            java.util.function.Function<ProfileRow, javafx.beans.value.ObservableValue<String>> property,
            double width,
            boolean centered
    ) {
        TableColumn<ProfileRow, String> column = new TableColumn<>(title);
        column.setCellValueFactory(data -> property.apply(data.getValue()));
        column.setPrefWidth(width);
        column.getStyleClass().add(centered ? "center-column" : "left-column");
        return column;
    }

    private TableColumn<ProfileRow, String> osColumn(String title, double width) {
        TableColumn<ProfileRow, String> column = textColumn(title, ProfileRow::osProperty, width, true);
        column.setCellFactory(_col -> new TableCell<>() {
            @Override
            protected void updateItem(String item, boolean empty) {
                super.updateItem(item, empty);
                if (empty || item == null || item.isBlank()) {
                    setGraphic(null);
                    setText(null);
                    return;
                }
                setGraphic(osGraphic(item));
                setText(null);
                setAlignment(Pos.CENTER);
            }
        });
        return column;
    }

    private Pane osGraphic(String kind) {
        String normalized = kind.toLowerCase();
        if (normalized.contains("windows")) {
            GridPane logo = new GridPane();
            logo.getStyleClass().add("windows-logo");
            logo.setHgap(2);
            logo.setVgap(2);
            for (int row = 0; row < 2; row++) {
                for (int column = 0; column < 2; column++) {
                    Rectangle tile = new Rectangle(6, 6);
                    tile.getStyleClass().add("windows-logo-tile");
                    logo.add(tile, column, row);
                }
            }
            return logo;
        }
        Label label = new Label(normalized.contains("mac") ? "mac" : "linux");
        label.getStyleClass().add("os-text-icon");
        return new StackPane(label);
    }

    private TableColumn<ProfileRow, String> proxyColumn(String title, double width) {
        TableColumn<ProfileRow, String> column = textColumn(title, ProfileRow::proxyProperty, width, false);
        column.setCellFactory(_col -> new TableCell<>() {
            private final Button checkButton = new Button("⇄");
            private final Label proxyText = new Label();
            private final Label checkPill = new Label();
            private final HBox box = new HBox(7, checkButton, proxyText, checkPill);

            {
                checkButton.getStyleClass().add("proxy-check-button");
                proxyText.getStyleClass().add("proxy-text");
                checkPill.getStyleClass().add("proxy-check-pill");
                box.setAlignment(Pos.CENTER_LEFT);
                checkButton.setOnAction(event -> {
                    ProfileRow row = rowAt(getIndex());
                    if (row != null) {
                        runAction("Проверка прокси", () -> api.checkProxy(List.of(row.getId())));
                    }
                    event.consume();
                });
            }

            @Override
            protected void updateItem(String item, boolean empty) {
                super.updateItem(item, empty);
                ProfileRow row = rowAt(getIndex());
                if (empty || row == null) {
                    setGraphic(null);
                    setText(null);
                    return;
                }
                proxyText.setText(item == null || item.isBlank() ? "Без прокси" : item);
                String checkLabel = row.rawString("proxy_check_label");
                String checkState = row.rawString("proxy_check_state");
                checkPill.setText(checkLabel);
                checkPill.getStyleClass().setAll("proxy-check-pill", proxyCheckStyle(checkState));
                checkPill.setVisible(!checkLabel.isBlank());
                checkPill.setManaged(!checkLabel.isBlank());
                setGraphic(box);
                setText(null);
                setAlignment(Pos.CENTER_LEFT);
            }
        });
        return column;
    }

    private ProfileRow rowAt(int index) {
        if (table == null || index < 0 || index >= table.getItems().size()) {
            return null;
        }
        return table.getItems().get(index);
    }

    private String proxyCheckStyle(String state) {
        return switch (state) {
            case "ok" -> "proxy-check-ok";
            case "blocked" -> "proxy-check-blocked";
            case "fail" -> "proxy-check-fail";
            case "unknown" -> "proxy-check-muted";
            default -> "proxy-check-muted";
        };
    }

    private TableColumn<ProfileRow, String> statusColumn(
            String title,
            java.util.function.Function<ProfileRow, javafx.beans.value.ObservableValue<String>> property,
            double width
    ) {
        TableColumn<ProfileRow, String> column = textColumn(title, property, width, true);
        column.setCellFactory(_col -> new TableCell<>() {
            private final Label pill = new Label();

            @Override
            protected void updateItem(String item, boolean empty) {
                super.updateItem(item, empty);
                if (empty || item == null || item.isBlank()) {
                    setGraphic(null);
                    setText(null);
                    return;
                }
                pill.setText(item);
                pill.getStyleClass().setAll("table-pill", isRunningStatus(item) ? "status-running-pill" : "status-stopped-pill");
                setGraphic(pill);
                setText(null);
                setAlignment(Pos.CENTER);
            }
        });
        return column;
    }

    private TableColumn<ProfileRow, String> pillColumn(
            String title,
            java.util.function.Function<ProfileRow, javafx.beans.value.ObservableValue<String>> property,
            double width,
            String pillStyle
    ) {
        TableColumn<ProfileRow, String> column = textColumn(title, property, width, true);
        column.setCellFactory(_col -> new TableCell<>() {
            private final Label pill = new Label();

            @Override
            protected void updateItem(String item, boolean empty) {
                super.updateItem(item, empty);
                if (empty || item == null || item.isBlank()) {
                    setGraphic(null);
                    setText(null);
                    return;
                }
                pill.setText(item);
                pill.getStyleClass().setAll("table-pill", pillStyle);
                setGraphic(pill);
                setText(null);
                setAlignment(Pos.CENTER);
            }
        });
        return column;
    }

    private boolean isRunningStatus(String value) {
        String normalized = value.toLowerCase();
        return normalized.contains("run") || normalized.contains("start") || normalized.contains("зап");
    }

    private Button button(String text, Runnable action) {
        Button button = new Button(text);
        button.setOnAction(_event -> action.run());
        return button;
    }

    private void refreshFolders() {
        CompletableFuture.supplyAsync(() -> {
            try {
                return api.folders();
            } catch (Exception exc) {
                throw new RuntimeException(exc);
            }
        }, executor).thenAccept(rows -> Platform.runLater(() -> {
            List<String> next = new ArrayList<>();
            next.add(ALL_FOLDERS);
            for (String row : rows) {
                if (!row.isBlank() && next.stream().noneMatch(item -> item.equalsIgnoreCase(row))) {
                    next.add(row);
                }
            }
            if (next.stream().noneMatch(item -> item.equalsIgnoreCase(selectedFolder))) {
                selectedFolder = next.stream().filter(DEFAULT_FOLDER::equalsIgnoreCase).findFirst().orElse(ALL_FOLDERS);
            }
            folders.setAll(next);
            folderList.getSelectionModel().select(selectedFolder);
            updateFolderTitle();
        })).exceptionally(exc -> {
            Platform.runLater(() -> statusLabel.setText("Ошибка папок: " + rootMessage(exc)));
            return null;
        });
    }

    private void showCreateFolderDialog() {
        TextInputDialog dialog = new TextInputDialog("Новая папка");
        dialog.setTitle("Добавить папку");
        dialog.setHeaderText("Новая папка");
        dialog.setContentText("Название");
        dialog.showAndWait()
                .map(String::trim)
                .filter(value -> !value.isBlank())
                .ifPresent(this::createFolder);
    }

    private void createFolder(String name) {
        statusLabel.setText("Создание папки...");
        CompletableFuture.runAsync(() -> {
            try {
                api.createFolder(name);
            } catch (Exception exc) {
                throw new RuntimeException(exc);
            }
        }, executor).thenRun(() -> Platform.runLater(() -> {
            selectedFolder = name.trim();
            refreshFolders();
            refreshProfiles();
        })).exceptionally(exc -> {
            Platform.runLater(() -> showError("Папка", rootMessage(exc)));
            return null;
        });
    }

    private String profileFilterFolder() {
        return ALL_FOLDERS.equals(selectedFolder) ? "" : selectedFolder;
    }

    private String createProfileFolder() {
        return ALL_FOLDERS.equals(selectedFolder) ? DEFAULT_FOLDER : selectedFolder;
    }

    private String folderTitle() {
        return selectedFolder == null || selectedFolder.isBlank() ? DEFAULT_FOLDER : selectedFolder;
    }

    private void updateFolderTitle() {
        if (titleLabel != null) {
            titleLabel.setText(folderTitle());
        }
    }

    private void refreshProfiles() {
        CompletableFuture.supplyAsync(() -> {
            try {
                return api.profiles(profileFilterFolder());
            } catch (Exception exc) {
                throw new RuntimeException(exc);
            }
        }, executor).thenAccept(rows -> Platform.runLater(() -> {
            Map<String, Boolean> selected = new LinkedHashMap<>();
            for (ProfileRow row : profiles) {
                selected.put(row.getId(), row.isSelected());
            }
            rows.forEach(row -> {
                row.setSelected(Boolean.TRUE.equals(selected.get(row.getId())));
                row.selectedProperty().addListener((_obs, _old, _value) -> updateSelectionCount());
            });
            profiles.setAll(rows);
            updateSelectionCount();
            statusLabel.setText("Профилей: " + profiles.size());
        })).exceptionally(exc -> {
            Platform.runLater(() -> statusLabel.setText("Ошибка обновления: " + rootMessage(exc)));
            return null;
        });
    }

    private void updateSelectionCount() {
        long selected = profiles.stream().filter(ProfileRow::isSelected).count();
        if (selectedCountLabel != null) {
            selectedCountLabel.setText("Выбрано: " + selected);
        }
        if (selectAllHeader != null) {
            updatingSelectionHeader = true;
            selectAllHeader.setIndeterminate(selected > 0 && selected < profiles.size());
            selectAllHeader.setSelected(!profiles.isEmpty() && selected == profiles.size());
            updatingSelectionHeader = false;
        }
    }

    private void runAction(String label, ThrowingRunnable action) {
        Collection<String> ids = selectedIds();
        statusLabel.setText(label + "...");
        CompletableFuture.runAsync(() -> {
            try {
                action.run();
            } catch (Exception exc) {
                throw new RuntimeException(exc);
            }
        }, executor).thenRun(this::refreshProfiles).exceptionally(exc -> {
            Platform.runLater(() -> showError(label, rootMessage(exc)));
            return null;
        });
    }

    private List<String> selectedIds() {
        List<String> ids = profiles.stream().filter(ProfileRow::isSelected).map(ProfileRow::getId).toList();
        if (!ids.isEmpty()) {
            return ids;
        }
        ProfileRow row = focusedOrFirstSelected();
        return row == null ? List.of() : List.of(row.getId());
    }

    private ProfileRow focusedOrFirstSelected() {
        ProfileRow focused = table.getSelectionModel().getSelectedItem();
        if (focused != null) {
            return focused;
        }
        return profiles.stream().filter(ProfileRow::isSelected).findFirst().orElse(null);
    }

    private void showProxyPoolDialog() {
        CompletableFuture.supplyAsync(() -> {
            try {
                return String.join(System.lineSeparator(), api.proxies());
            } catch (Exception exc) {
                throw new RuntimeException(exc);
            }
        }, executor).thenAccept(text -> Platform.runLater(() -> {
            Dialog<ButtonType> dialog = new Dialog<>();
            dialog.setTitle("Proxy pool");
            TextArea area = new TextArea(text);
            area.setPrefSize(680, 420);
            dialog.getDialogPane().setContent(area);
            ButtonType save = new ButtonType("Сохранить", ButtonBar.ButtonData.OK_DONE);
            dialog.getDialogPane().getButtonTypes().addAll(save, ButtonType.CANCEL);
            dialog.showAndWait().filter(save::equals).ifPresent(_button -> runAction("Proxy pool", () -> api.saveProxies(area.getText())));
        })).exceptionally(exc -> {
            Platform.runLater(() -> showError("Proxy pool", rootMessage(exc)));
            return null;
        });
    }

    private void showNewProfileDialog() {
        if (shell == null) {
            return;
        }

        StackPane overlay = new StackPane();
        overlay.getStyleClass().add("modal-overlay");

        TextField name = modalField("Профиль " + (profiles.size() + 1), "Название профиля");
        TextField folder = modalField(createProfileFolder(), "Папка");
        TextField tags = modalField("", "Теги");
        TextField startUrl = modalField("https://browserleaks.com/ip", "Стартовая страница");
        TextField browserPath = modalField("", "Путь браузера");
        TextField localPort = modalField("", "Локальный порт");
        TextField proxyType = modalField("http", "http / socks5");
        TextField proxyHost = modalField("", "Host");
        TextField proxyPort = modalField("", "Port");
        TextField proxyLogin = modalField("", "Login");
        TextField proxyPassword = modalField("", "Password");

        Label nameSummary = new Label();
        Label folderSummary = new Label();
        Label proxySummary = new Label();
        Label platformSummary = new Label("Windows");
        Label portSummary = new Label("авто");

        Runnable updateSummary = () -> {
            nameSummary.setText(name.getText().isBlank() ? "Новый профиль" : name.getText().trim());
            folderSummary.setText(folder.getText().isBlank() ? DEFAULT_FOLDER : folder.getText().trim());
            proxySummary.setText(proxyPreview(proxyType.getText(), proxyHost.getText(), proxyPort.getText(), proxyLogin.getText()));
            portSummary.setText(localPort.getText().isBlank() ? "авто" : localPort.getText().trim());
        };
        List.of(name, folder, proxyType, proxyHost, proxyPort, proxyLogin, localPort).forEach(field ->
                field.textProperty().addListener((_obs, _old, _value) -> updateSummary.run()));
        updateSummary.run();

        Label title = new Label("Новый профиль");
        title.getStyleClass().add("modal-title");
        Label tab = new Label("Основное");
        tab.getStyleClass().add("modal-tab-active");
        Button close = button("×", () -> closeModal(overlay));
        close.getStyleClass().add("modal-close-button");
        HBox header = new HBox(16, title, spacer(), close);
        header.setAlignment(Pos.CENTER_LEFT);

        HBox tabs = new HBox(22, tab);
        tabs.getStyleClass().add("modal-tabs");

        GridPane form = new GridPane();
        form.getStyleClass().add("modal-form");
        form.setHgap(14);
        form.setVgap(12);
        addModalField(form, 0, 0, "Название профиля", name);
        addModalField(form, 0, 1, "Папка", folder);
        addModalField(form, 1, 0, "Теги", tags);
        addModalField(form, 1, 1, "Стартовая страница", startUrl);
        addModalField(form, 2, 0, "Путь браузера", browserPath);
        addModalField(form, 2, 1, "Локальный порт", localPort);
        addModalField(form, 3, 0, "Тип прокси", proxyType);
        addModalField(form, 3, 1, "Host", proxyHost);
        addModalField(form, 4, 0, "Port", proxyPort);
        addModalField(form, 4, 1, "Login", proxyLogin);
        addModalField(form, 5, 0, "Password", proxyPassword);

        VBox summary = new VBox(
                14,
                new Label("СВОДКА"),
                summaryLine("Название", nameSummary),
                summaryLine("Папка", folderSummary),
                summaryLine("Платформа", platformSummary),
                summaryLine("Прокси", proxySummary),
                summaryLine("Лок. порт", portSummary)
        );
        summary.getStyleClass().add("modal-summary");

        HBox body = new HBox(24, form, summary);
        body.getStyleClass().add("modal-body");
        HBox.setHgrow(form, Priority.ALWAYS);

        Button cancel = button("ОТМЕНА", () -> closeModal(overlay));
        cancel.getStyleClass().add("modal-cancel-button");
        Button create = button("СОЗДАТЬ ПРОФИЛЬ", () -> createProfileFromDialog(
                overlay,
                name,
                folder,
                tags,
                startUrl,
                browserPath,
                localPort,
                proxyType,
                proxyHost,
                proxyPort,
                proxyLogin,
                proxyPassword
        ));
        create.getStyleClass().add("modal-create-button");
        HBox footer = new HBox(14, spacer(), cancel, create);
        footer.setAlignment(Pos.CENTER_RIGHT);
        footer.getStyleClass().add("modal-footer");

        VBox card = new VBox(14, header, tabs, body, footer);
        card.getStyleClass().add("profile-modal");
        overlay.getChildren().add(card);
        StackPane.setMargin(card, new Insets(24));
        shell.getChildren().add(overlay);
        name.requestFocus();
    }

    private Region spacer() {
        Region spacer = new Region();
        HBox.setHgrow(spacer, Priority.ALWAYS);
        return spacer;
    }

    private TextField modalField(String value, String prompt) {
        TextField field = field(value);
        field.setPromptText(prompt);
        field.getStyleClass().add("modal-field");
        return field;
    }

    private void addModalField(GridPane grid, int row, int column, String label, TextField field) {
        VBox box = new VBox(5, new Label(label), field);
        box.getStyleClass().add("modal-field-box");
        grid.add(box, column, row);
        GridPane.setHgrow(box, Priority.ALWAYS);
    }

    private HBox summaryLine(String title, Label value) {
        Label key = new Label(title);
        key.getStyleClass().add("summary-key");
        value.getStyleClass().add("summary-value");
        HBox line = new HBox(14, key, value);
        line.setAlignment(Pos.TOP_LEFT);
        return line;
    }

    private String proxyPreview(String type, String host, String port, String login) {
        if (host == null || host.isBlank() || port == null || port.isBlank()) {
            return "Без прокси";
        }
        String auth = login == null || login.isBlank() ? "" : login.trim() + ":***@";
        String scheme = type == null || type.isBlank() ? "http" : type.trim().toLowerCase();
        return scheme + "://" + auth + host.trim() + ":" + port.trim();
    }

    private void closeModal(StackPane overlay) {
        shell.getChildren().remove(overlay);
    }

    private void createProfileFromDialog(
            StackPane overlay,
            TextField name,
            TextField folder,
            TextField tags,
            TextField startUrl,
            TextField browserPath,
            TextField localPort,
            TextField proxyType,
            TextField proxyHost,
            TextField proxyPort,
            TextField proxyLogin,
            TextField proxyPassword
    ) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("name", name.getText());
        payload.put("folder", folder.getText());
        payload.put("notes", tags.getText());
        payload.put("start_url", startUrl.getText());
        payload.put("browser_path", browserPath.getText());
        payload.put("local_port", localPort.getText());
        payload.put("proxy_type", proxyType.getText());
        payload.put("proxy_host", proxyHost.getText());
        payload.put("proxy_port", proxyPort.getText());
        payload.put("proxy_login", proxyLogin.getText());
        payload.put("proxy_password", proxyPassword.getText());

        statusLabel.setText("Создание профиля...");
        CompletableFuture.runAsync(() -> {
            try {
                api.createProfile(payload);
            } catch (Exception exc) {
                throw new RuntimeException(exc);
            }
        }, executor).thenRun(() -> Platform.runLater(() -> {
            closeModal(overlay);
            selectedFolder = folder.getText().isBlank() ? DEFAULT_FOLDER : folder.getText().trim();
            refreshFolders();
            refreshProfiles();
        })).exceptionally(exc -> {
            Platform.runLater(() -> showError("Создание профиля", rootMessage(exc)));
            return null;
        });
    }

    private void showProfileDialog(ProfileRow row) {
        TextField name = field(row.rawString("name"));
        TextField folder = field(row.rawString("folder").isBlank() ? createProfileFolder() : row.rawString("folder"));
        TextField startUrl = field(row.rawString("start_url"));
        TextField browserPath = field(row.rawString("browser_path"));
        TextField localPort = field(row.rawString("local_port"));
        TextField notes = field(row.rawString("notes"));
        TextField proxyType = field(row.rawString("proxy_type"));
        TextField proxyHost = field(row.rawString("proxy_host"));
        TextField proxyPort = field(row.rawString("proxy_port"));
        TextField proxyLogin = field(row.rawString("proxy_login"));
        TextField proxyPassword = field(row.rawString("proxy_password"));

        GridPane grid = new GridPane();
        grid.setHgap(12);
        grid.setVgap(10);
        grid.setPadding(new Insets(16));
        addRow(grid, 0, "Название", name, "Папка", folder);
        addRow(grid, 1, "Стартовая", startUrl, "Лок. порт", localPort);
        addRow(grid, 2, "Путь браузера", browserPath, "Теги", notes);
        addRow(grid, 3, "Proxy type", proxyType, "Host", proxyHost);
        addRow(grid, 4, "Port", proxyPort, "Login", proxyLogin);
        grid.add(new Label("Password"), 0, 5);
        grid.add(proxyPassword, 1, 5);

        Dialog<ButtonType> dialog = new Dialog<>();
        dialog.setTitle("Редактировать профиль " + row.getName());
        dialog.getDialogPane().setContent(grid);
        ButtonType save = new ButtonType("Сохранить", ButtonBar.ButtonData.OK_DONE);
        dialog.getDialogPane().getButtonTypes().addAll(save, ButtonType.CANCEL);
        dialog.showAndWait().filter(save::equals).ifPresent(_button -> {
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("id", row.getId());
            payload.put("name", name.getText());
            payload.put("folder", folder.getText());
            payload.put("start_url", startUrl.getText());
            payload.put("browser_path", browserPath.getText());
            payload.put("local_port", localPort.getText());
            payload.put("notes", notes.getText());
            payload.put("proxy_type", proxyType.getText());
            payload.put("proxy_host", proxyHost.getText());
            payload.put("proxy_port", proxyPort.getText());
            payload.put("proxy_login", proxyLogin.getText());
            payload.put("proxy_password", proxyPassword.getText());
            runAction("Сохранение профиля", () -> api.updateProfile(payload));
        });
    }

    private TextField field(String value) {
        TextField field = new TextField(value);
        field.setMinWidth(220);
        return field;
    }

    private void addRow(GridPane grid, int row, String leftLabel, TextField left, String rightLabel, TextField right) {
        grid.add(new Label(leftLabel), 0, row);
        grid.add(left, 1, row);
        grid.add(new Label(rightLabel), 2, row);
        grid.add(right, 3, row);
    }

    private void startPolling() {
        poller = new Timeline(new KeyFrame(Duration.seconds(1), _event -> {
            refreshProfiles();
        }));
        poller.setCycleCount(Timeline.INDEFINITE);
        poller.play();
    }

    private void startBackendIfNeeded() {
        if (api.isHealthy()) {
            return;
        }
        Path root = Paths.get(System.getProperty("browser.manager.root", System.getProperty("user.dir"))).toAbsolutePath();
        String python = System.getProperty("browser.manager.python", "python");
        ProcessBuilder builder = new ProcessBuilder(
                python,
                "backend_server.py",
                "--host",
                "127.0.0.1",
                "--port",
                String.valueOf(API_PORT)
        );
        builder.directory(root.toFile());
        builder.redirectErrorStream(true);
        try {
            backendProcess = builder.start();
            Thread reader = new Thread(() -> readBackendOutput(backendProcess), "browser-manager-backend-log");
            reader.setDaemon(true);
            reader.start();
            for (int i = 0; i < 40; i++) {
                if (api.isHealthy()) {
                    return;
                }
                Thread.sleep(200);
            }
            showError("Backend", "Python backend не ответил на порту " + API_PORT);
        } catch (Exception exc) {
            showError("Backend", rootMessage(exc));
        }
    }

    private void readBackendOutput(Process process) {
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                String message = line;
                Platform.runLater(() -> {
                    if (statusLabel != null) {
                        statusLabel.setText(message);
                    }
                });
            }
        } catch (IOException ignored) {
        }
    }

    private void addStylesheet(Scene scene) {
        Path root = Paths.get(System.getProperty("browser.manager.root", System.getProperty("user.dir"))).toAbsolutePath();
        Path css = root.resolve("javafx-client/src/main/resources/browsermanager/app.css");
        if (css.toFile().exists()) {
            scene.getStylesheets().add(css.toUri().toString());
        }
    }

    private void showError(String title, String message) {
        Alert alert = new Alert(Alert.AlertType.ERROR);
        alert.setTitle(title);
        alert.setHeaderText(title);
        alert.setContentText(message);
        alert.showAndWait();
    }

    private String rootMessage(Throwable throwable) {
        Throwable current = throwable;
        while (current.getCause() != null && !Objects.equals(current, current.getCause())) {
            current = current.getCause();
        }
        return current.getMessage() == null ? current.toString() : current.getMessage();
    }

    public static void main(String[] args) {
        launch(args);
    }

    @FunctionalInterface
    private interface ThrowingRunnable {
        void run() throws Exception;
    }
}
