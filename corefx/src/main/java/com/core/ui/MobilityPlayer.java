package com.core.ui;

import com.core.Controller;
import com.core.data.CoreNode;
import com.core.data.MobilityConfig;
import com.jfoenix.controls.JFXButton;
import javafx.fxml.FXML;
import javafx.fxml.FXMLLoader;
import javafx.scene.control.Label;
import javafx.scene.layout.HBox;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.io.IOException;

public class MobilityPlayer extends HBox {
    private static final Logger logger = LogManager.getLogger();

    @FXML
    private Label label;

    @FXML
    private JFXButton playButton;

    @FXML
    private JFXButton pauseButton;

    @FXML
    private JFXButton stopButton;

    private Controller controller;
    private CoreNode node;
    private MobilityConfig mobilityConfig;

    public MobilityPlayer(Controller controller) {
        this.controller = controller;
        FXMLLoader loader = new FXMLLoader(getClass().getResource("/fxml/mobility_player.fxml"));
        loader.setRoot(this);
        loader.setController(this);

        try {
            loader.load();
        } catch (IOException ex) {
            throw new RuntimeException(ex);
        }

        playButton.setOnAction(event -> action("start"));
        pauseButton.setOnAction(event -> action("pause"));
        stopButton.setOnAction(event -> action("stop"));
    }

    private void action(String action) {
        try {
            controller.getCoreClient().mobilityAction(node, action);
        } catch (IOException ex) {
            Toast.error(String.format("mobility error: %s", action), ex);
        }
    }

    public void show(CoreNode node, MobilityConfig mobilityConfig) {
        this.node = node;
        this.mobilityConfig = mobilityConfig;
        label.setText(String.format("%s - %s", node.getName(), mobilityConfig.getFile()));
    }
}