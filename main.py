import sys
import json
import time
import csv
import os
import threading
import logging
from datetime import datetime

import serial

try:
    import yaml
except ImportError:
    yaml = None

try:
    from mcap.writer import Writer as McapWriter
    MCAP_AVAILABLE = True
except ImportError:
    McapWriter = None
    MCAP_AVAILABLE = False

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QHeaderView, QScrollArea, QLineEdit, QPushButton,
    QListWidget, QSizePolicy, QGridLayout, QFrame
)
from PyQt5.QtWidgets import QAbstractScrollArea
from PyQt5.QtGui import QColor
from PyQt5.QtCore import Qt, QObject, QThread, pyqtSignal


# ---------------------------------------------------------------------
# Basic program logging: console messages for errors/info/debug
# ---------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger("serial_json_monitor")


# ---------------------------------------------------------------------
# Data logger: logs received JSON values and sent commands
# ---------------------------------------------------------------------

class SerialDataLogger:
    """
    Logs serial traffic.

    CSV mode:
        One row per JSON value.
        Format:
            timestamp_iso,direction,port,path,value,raw_json

    MCAP mode:
        One MCAP message per received JSON message or sent command.
        Requires:
            pip install mcap
    """

    def __init__(self, log_dir="logs", log_format="csv", prefix="serial_log"):
        self.log_dir = log_dir or "logs"
        self.log_format = str(log_format or "csv").lower().strip()
        self.prefix = prefix or "serial_log"
        self.lock = threading.RLock()
        self.closed = False

        os.makedirs(self.log_dir, exist_ok=True)

        start_stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        self.csv_file = None
        self.csv_writer = None

        self.mcap_file = None
        self.mcap_writer = None
        self.mcap_channel_id = None

        if self.log_format == "mcap":
            if MCAP_AVAILABLE:
                try:
                    self._open_mcap(start_stamp)
                    logger.info("MCAP serial log file: %s", self.path)
                    return
                except Exception as e:
                    logger.error("Could not start MCAP logging: %s", e)
                    logger.warning("Falling back to CSV logging.")
            else:
                logger.warning("MCAP package not installed. Falling back to CSV.")
                logger.warning("Install with: pip install mcap")

        self.log_format = "csv"
        self._open_csv(start_stamp)
        logger.info("CSV serial log file: %s", self.path)

    def _open_csv(self, start_stamp):
        self.path = os.path.join(self.log_dir, f"{self.prefix}_{start_stamp}.csv")
        self.csv_file = open(self.path, "w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow([
            "timestamp_iso",
            "direction",
            "port",
            "path",
            "value",
            "raw_json"
        ])
        self.csv_file.flush()

    def _open_mcap(self, start_stamp):
        self.path = os.path.join(self.log_dir, f"{self.prefix}_{start_stamp}.mcap")
        self.mcap_file = open(self.path, "wb")
        self.mcap_writer = McapWriter(self.mcap_file)
        self.mcap_writer.start(
            profile="jsonschema",
            library="pyqt_serial_json_monitor"
        )

        schema = {
            "type": "object",
            "properties": {
                "timestamp_iso": {"type": "string"},
                "direction": {"type": "string"},
                "port": {"type": "string"},
                "path": {"type": "string"},
                "value": {},
                # "raw_json": {}
            }
        }

        schema_id = self.mcap_writer.register_schema(
            name="SerialEvent",
            encoding="jsonschema",
            data=json.dumps(schema).encode("utf-8")
        )

        self.mcap_channel_id = self.mcap_writer.register_channel(
            topic="/serial/events",
            message_encoding="json",
            schema_id=schema_id
        )

    def _flatten_json(self, data, prefix=""):
        """
        Turns nested JSON into path/value pairs.

        Example:
            {"a": {"b": 5}, "arr": [10, 20]}

        Becomes:
            a.b = 5
            arr[0] = 10
            arr[1] = 20
        """
        values = []

        if isinstance(data, dict):
            for key, value in data.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                values.extend(self._flatten_json(value, path))

        elif isinstance(data, list):
            for index, value in enumerate(data):
                path = f"{prefix}[{index}]"
                values.extend(self._flatten_json(value, path))

        else:
            values.append((prefix, data))

        return values

    def _write_csv_row(self, timestamp_iso, direction, port, path, value, raw_json):
        self.csv_writer.writerow([
            timestamp_iso,
            direction,
            port,
            path,
            value,
            # raw_json
        ])
        self.csv_file.flush()

    def _write_mcap_event(self, event):
        now_ns = time.time_ns()
        data = json.dumps(event, ensure_ascii=False).encode("utf-8")

        self.mcap_writer.add_message(
            channel_id=self.mcap_channel_id,
            log_time=now_ns,
            publish_time=now_ns,
            data=data
        )

    def log_received(self, port, data):
        """
        Log received JSON.

        In CSV mode:
            Logs every scalar JSON value as a separate comma-separated row.

        In MCAP mode:
            Logs the whole JSON message as one MCAP event.
        """
        timestamp_iso = datetime.now().isoformat(timespec="milliseconds")
        raw_json = json.dumps(data, ensure_ascii=False)

        with self.lock:
            if self.closed:
                return

            if self.log_format == "mcap":
                event = {
                    "timestamp_iso": timestamp_iso,
                    "direction": "receive",
                    "port": port,
                    "path": "",
                    "value": None,
                    "raw_json": data
                }
                self._write_mcap_event(event)
                return

            flattened = self._flatten_json(data)

            if not flattened:
                self._write_csv_row(
                    timestamp_iso,
                    "receive",
                    port,
                    "",
                    "",
                    raw_json
                )
                return

            for path, value in flattened:
                self._write_csv_row(
                    timestamp_iso,
                    "receive",
                    port,
                    path,
                    value,
                    raw_json
                )

    def log_send(self, port, command):
        timestamp_iso = datetime.now().isoformat(timespec="milliseconds")

        with self.lock:
            if self.closed:
                return

            if self.log_format == "mcap":
                event = {
                    "timestamp_iso": timestamp_iso,
                    "direction": "send",
                    "port": port,
                    "path": "command",
                    "value": command,
                    "raw_json": {
                        "command": command
                    }
                }
                self._write_mcap_event(event)
                return

            self._write_csv_row(
                timestamp_iso,
                "send",
                port,
                "command",
                command,
                json.dumps({"command": command}, ensure_ascii=False)
            )

    def log_send_failed(self, port, command, error):
        timestamp_iso = datetime.now().isoformat(timespec="milliseconds")

        with self.lock:
            if self.closed:
                return

            if self.log_format == "mcap":
                event = {
                    "timestamp_iso": timestamp_iso,
                    "direction": "send_failed",
                    "port": port,
                    "path": "command",
                    "value": command,
                    "raw_json": {
                        "command": command,
                        "error": str(error)
                    }
                }
                self._write_mcap_event(event)
                return

            self._write_csv_row(
                timestamp_iso,
                "send_failed",
                port,
                "command",
                command,
                json.dumps(
                    {
                        "command": command,
                        "error": str(error)
                    },
                    ensure_ascii=False
                )
            )

    def close(self):
        with self.lock:
            if self.closed:
                return

            self.closed = True

            try:
                if self.mcap_writer:
                    self.mcap_writer.finish()
            except Exception as e:
                logger.warning("Error finishing MCAP log: %s", e)

            try:
                if self.mcap_file:
                    self.mcap_file.close()
            except Exception:
                pass

            try:
                if self.csv_file:
                    self.csv_file.flush()
                    self.csv_file.close()
            except Exception:
                pass


# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------

def load_config(path="config.yaml"):
    """Load YAML configuration if available. Returns dict with defaults."""
    cfg = {
        "COM": "/dev/ttyUSB0",
        "BAUD": 500000,
        "buttons": [],
        "precision": 3,

        # Logging
        "log_dir": "logs",
        "log_format": "csv",       # "csv" or "mcap"
        "log_prefix": "serial_log",

        # Preferred: per-table rules
        "heatmaps": None,

        # Legacy fallbacks
        "heatmap_tables": None,
        "max_deviation": 0.05
    }

    if not path:
        return cfg

    try:
        if yaml is None:
            logger.warning("PyYAML not installed; using defaults. Install with: pip install pyyaml")
            return cfg

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        if not isinstance(data, dict):
            return cfg

        if "COM" in data and isinstance(data["COM"], str) and data["COM"]:
            cfg["COM"] = data["COM"]

        if "BAUD" in data:
            try:
                cfg["BAUD"] = int(data["BAUD"])
            except Exception:
                logger.warning("Invalid BAUD in YAML; using default 500000")

        if "log_dir" in data and isinstance(data["log_dir"], str) and data["log_dir"]:
            cfg["log_dir"] = data["log_dir"]

        if "log_format" in data and isinstance(data["log_format"], str):
            fmt = data["log_format"].lower().strip()
            if fmt in ("csv", "mcap"):
                cfg["log_format"] = fmt
            else:
                logger.warning("Invalid log_format in YAML; using csv")

        if "log_prefix" in data and isinstance(data["log_prefix"], str) and data["log_prefix"]:
            cfg["log_prefix"] = data["log_prefix"]

        if "buttons" in data and isinstance(data["buttons"], list):
            norm = []
            for item in data["buttons"]:
                if not isinstance(item, dict):
                    continue

                name = str(item.get("name", "")).strip()
                value = str(item.get("value", "")).strip()

                if name and value:
                    norm.append({"name": name, "value": value})
                else:
                    logger.warning("Skipping button with missing name/value: %s", item)

            cfg["buttons"] = norm

        prec_key = "precision" if "precision" in data else ("PRECISION" if "PRECISION" in data else None)

        if prec_key is not None:
            try:
                p = int(data[prec_key])
                if p < 0:
                    raise ValueError
                cfg["precision"] = p
            except Exception:
                logger.warning("Invalid precision in YAML; using default 3")

        if "heatmaps" in data and isinstance(data["heatmaps"], list):
            hm = []

            for entry in data["heatmaps"]:
                if isinstance(entry, dict):
                    name = str(entry.get("name", "")).strip()

                    try:
                        md = float(entry.get("max_deviation", cfg["max_deviation"]))
                    except Exception:
                        md = cfg["max_deviation"]

                    if name:
                        hm.append({
                            "name": name,
                            "max_deviation": max(0.0, md)
                        })

            cfg["heatmaps"] = hm if hm else None

        if cfg["heatmaps"] is None:
            if "heatmap_tables" in data:
                ht = data["heatmap_tables"]

                if isinstance(ht, str):
                    cfg["heatmap_tables"] = [ht]
                elif isinstance(ht, list):
                    cfg["heatmap_tables"] = [str(x) for x in ht if x]
                else:
                    cfg["heatmap_tables"] = None

            if "max_deviation" in data:
                try:
                    md = float(data["max_deviation"])
                    if md < 0:
                        raise ValueError
                    cfg["max_deviation"] = md
                except Exception:
                    logger.warning("Invalid max_deviation in YAML; using default 0.05")

    except FileNotFoundError:
        pass

    except Exception as e:
        logger.warning("Failed to load YAML config: %s", e)

    return cfg


# ---------------------------------------------------------------------
# Serial worker
# ---------------------------------------------------------------------

class SerialWorker(QObject):
    data_received = pyqtSignal(dict)

    def __init__(self, port="/dev/ttyUSB0", baudrate=500000, data_logger=None):
        super().__init__()

        self.port = port
        self.baudrate = baudrate
        self.data_logger = data_logger

        self._running = True
        self.serial_port = None
        self.reconnect_delay = 1.0
        self._lock = threading.RLock()

    def _close_port(self):
        """Immediately disconnect from the current serial port."""
        with self._lock:
            sp = self.serial_port
            self.serial_port = None

            if sp:
                try:
                    if sp.is_open:
                        sp.close()
                        logger.info("Closed serial port %s", self.port)
                except Exception as e:
                    logger.warning("Error while closing serial port: %s", e)

    def _open_port(self):
        """Try to open the configured serial port."""
        with self._lock:
            self.serial_port = serial.Serial(
                self.port,
                self.baudrate,
                timeout=0.2,
                write_timeout=0.5
            )

            logger.info("Opened serial %s @ %s", self.port, self.baudrate)

    def _wait_before_reconnect(self):
        """Wait a little before retrying, but allow fast shutdown."""
        steps = int(self.reconnect_delay * 10)

        for _ in range(max(1, steps)):
            if not self._running:
                return
            time.sleep(0.1)

    def start(self):
        buffer = ""

        while self._running:
            if self.serial_port is None or not self.serial_port.is_open:
                try:
                    self._open_port()
                    buffer = ""

                except serial.SerialException as e:
                    logger.error("Could not open serial port %s: %s", self.port, e)
                    self._close_port()
                    self._wait_before_reconnect()
                    continue

                except OSError as e:
                    logger.error("OS error opening serial port %s: %s", self.port, e)
                    self._close_port()
                    self._wait_before_reconnect()
                    continue

                except Exception as e:
                    logger.error("Unexpected error opening serial port %s: %s", self.port, e)
                    self._close_port()
                    self._wait_before_reconnect()
                    continue

            try:
                with self._lock:
                    if self.serial_port is None or not self.serial_port.is_open:
                        continue

                    line = self.serial_port.readline().decode(
                        "utf-8",
                        errors="ignore"
                    )

                if not line:
                    continue

                buffer = line.strip()

                try:
                    json_data = json.loads(buffer)

                    if self.data_logger:
                        self.data_logger.log_received(self.port, json_data)

                    self.data_received.emit(json_data)
                    buffer = ""

                except json.JSONDecodeError:
                    logger.debug("Ignoring non-JSON serial line: %s", buffer)
                    continue

            except serial.SerialException as e:
                logger.error("Serial port read error: %s", e)
                self._close_port()
                self._wait_before_reconnect()

            except OSError as e:
                logger.error("Serial OS read error: %s", e)
                self._close_port()
                self._wait_before_reconnect()

            except Exception as e:
                logger.error("Unexpected serial read error: %s", e)
                self._close_port()
                self._wait_before_reconnect()

    def stop(self):
        self._running = False
        self._close_port()

    def send_command(self, cmd):
        cmd = str(cmd).strip()

        if not cmd:
            return False

        with self._lock:
            if self.serial_port is None or not self.serial_port.is_open:
                error = f"serial port {self.port} is disconnected"
                logger.warning("Cannot send command; %s", error)

                if self.data_logger:
                    self.data_logger.log_send_failed(self.port, cmd, error)

                return False

            try:
                self.serial_port.write((cmd + "\n").encode("utf-8"))
                self.serial_port.flush()

                logger.info("Sent command: %s", cmd)

                if self.data_logger:
                    self.data_logger.log_send(self.port, cmd)

                return True

            except serial.SerialException as e:
                logger.error("Serial write error: %s", e)

                if self.data_logger:
                    self.data_logger.log_send_failed(self.port, cmd, e)

                self._close_port()
                return False

            except OSError as e:
                logger.error("Serial write OS error: %s", e)

                if self.data_logger:
                    self.data_logger.log_send_failed(self.port, cmd, e)

                self._close_port()
                return False

            except Exception as e:
                logger.error("Unexpected write error: %s", e)

                if self.data_logger:
                    self.data_logger.log_send_failed(self.port, cmd, e)

                self._close_port()
                return False


# ---------------------------------------------------------------------
# Table viewer
# ---------------------------------------------------------------------

class TableViewer(QWidget):
    def __init__(self, precision=3, heatmap_rules=None, default_max_dev=0.05):
        """
        heatmap_rules:
            dict[str, float] mapping table name -> max_deviation.

        If None:
            heatmap is applied to all tables using default_max_dev.
        """
        super().__init__()

        self.precision = int(precision) if precision is not None else 3
        self.heatmap_rules = dict(heatmap_rules) if heatmap_rules else None
        self.default_max_dev = float(default_max_dev) if default_max_dev is not None else 0.05

        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(5, 5, 5, 5)
        content_layout.setSpacing(5)

        scroll.setWidget(content)
        layout.addWidget(scroll)

        self.scroll = scroll
        self.content = content
        self.content_layout = content_layout
        self.last_rendered = None

    def display_tables(self, data):
        if data == self.last_rendered:
            return

        self.clear_layout(self.content_layout)
        self.render_data(data)
        self.last_rendered = data

    def render_data(self, data):
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, list) and self.is_2d_array(value):
                    self.add_label(key + ":", bold=True)

                    max_dev = None

                    if self.heatmap_rules is None:
                        max_dev = self.default_max_dev
                    elif key in self.heatmap_rules:
                        max_dev = self.heatmap_rules[key]

                    self.add_table(value, max_dev=max_dev)

                elif isinstance(value, (dict, list)):
                    self.render_data(value)

        elif isinstance(data, list):
            for item in data:
                self.render_data(item)

    def is_2d_array(self, arr):
        return (
            isinstance(arr, list)
            and arr
            and all(isinstance(r, list) for r in arr)
            and all(len(r) == len(arr[0]) for r in arr)
        )

    def add_label(self, text, bold=False):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        if bold:
            label.setStyleSheet("font-weight: bold")

        self.content_layout.addWidget(label)

    def _format_for_display(self, val):
        """Format numeric values with up to `precision` decimals."""
        try:
            num = float(val)
            s = f"{num:.{self.precision}f}"

            if self.precision > 0:
                s = s.rstrip("0").rstrip(".")
            else:
                s = s.split(".")[0]

            return s

        except Exception:
            return str(val)

    @staticmethod
    def _lerp(a, b, t):
        return int(a + (b - a) * max(0.0, min(1.0, t)))

    @staticmethod
    def _qcolor_from_rgb(r, g, b):
        return QColor(int(r), int(g), int(b))

    def _green_color(self, t):
        r = self._lerp(234, 184, t)
        g = self._lerp(251, 240, t)
        b = self._lerp(234, 184, t)
        return self._qcolor_from_rgb(r, g, b)

    def _red_color(self, t):
        r = self._lerp(255, 255, t)
        g = self._lerp(234, 140, t)
        b = self._lerp(234, 140, t)
        return self._qcolor_from_rgb(r, g, b)

    def _violet_color(self, t):
        r = self._lerp(255, 255, t)
        g = self._lerp(234, 140, t)
        b = self._lerp(234, 240, t)
        return self._qcolor_from_rgb(r, g, b)

    def add_table(self, table_data, max_dev=None):
        rows, cols = len(table_data), len(table_data[0])

        nums = []

        for r in table_data:
            for v in r:
                try:
                    nums.append(float(v))
                except Exception:
                    pass

        avg = sum(nums) / len(nums) if nums else 0.0

        table = QTableWidget(rows, cols)
        table.setVerticalHeaderLabels([str(i + 1) for i in range(rows)])
        table.verticalHeader().setVisible(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.setSizeAdjustPolicy(QAbstractScrollArea.AdjustToContents)

        red_cap_factor = 5.0

        for i in range(rows):
            for j in range(cols):
                raw_val = table_data[i][j]
                display_text = self._format_for_display(raw_val)

                item = QTableWidgetItem(display_text)

                if max_dev is not None and avg != 0:
                    try:
                        num = float(raw_val)

                        diff_abs = abs(num - avg) / abs(avg)
                        diff = num - avg

                        if diff <= max_dev:
                            t = (diff_abs / max_dev) * -1 + 1
                            color = self._green_color(t)
                            item.setBackground(color)

                        elif diff > max_dev:
                            over = diff_abs - max_dev
                            denom = max(max_dev * red_cap_factor, 1e-12)
                            t = max(0.0, min(1.0, over / denom))
                            color = self._red_color(t)
                            item.setBackground(color)

                        else:
                            over = diff_abs - max_dev
                            denom = max(max_dev * red_cap_factor, 1e-12)
                            t = max(0.0, min(1.0, over / denom))
                            color = self._violet_color(t)
                            item.setBackground(color)

                    except Exception:
                        pass

                table.setItem(i, j, item)

        table.resizeColumnsToContents()

        height = (
            sum(table.rowHeight(i) for i in range(rows))
            + table.horizontalHeader().height()
        )

        table.setFixedHeight(height)
        table.setStyleSheet("QTableWidget { border: 1px solid #ccc; }")

        self.content_layout.addWidget(table)

    def clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()

            if w:
                w.setParent(None)


# ---------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------

class App(QWidget):
    def __init__(
        self,
        port="/dev/ttyUSB0",
        baudrate=500000,
        buttons=None,
        precision=3,
        heatmaps=None,
        legacy_tables=None,
        legacy_max_dev=0.05,
        data_logger=None
    ):
        super().__init__()

        self.setWindowTitle("Serial JSON Monitor")
        self.setMinimumSize(1200, 600)

        self.data_logger = data_logger

        rules = None

        if heatmaps:
            rules = {
                str(h["name"]): float(h.get("max_deviation", legacy_max_dev))
                for h in heatmaps
                if "name" in h
            }

        elif legacy_tables:
            rules = {
                str(name): float(legacy_max_dev)
                for name in legacy_tables
            }

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(5, 5, 5, 5)
        main_layout.setSpacing(5)

        # Table section
        self.table_viewer = TableViewer(
            precision=precision,
            heatmap_rules=rules,
            default_max_dev=legacy_max_dev
        )

        self.table_viewer.setMinimumWidth(1100)
        self.table_viewer.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        main_layout.addWidget(self.table_viewer)

        # Non-table section
        self.non_table_display = QScrollArea()
        self.non_table_display.setWidgetResizable(True)
        self.non_table_display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.non_table_content = QWidget()
        self.non_table_layout = QVBoxLayout(self.non_table_content)
        self.non_table_layout.setContentsMargins(5, 5, 5, 5)
        self.non_table_layout.setSpacing(5)

        self.non_table_display.setWidget(self.non_table_content)

        main_layout.addWidget(self.non_table_display)

        # Command section
        cmd_input = QLineEdit()
        cmd_btn = QPushButton("Send")

        cmd_btn.clicked.connect(self.send_command)
        cmd_input.returnPressed.connect(self.send_command)

        cmd_layout = QVBoxLayout()
        cmd_layout.setContentsMargins(5, 5, 5, 5)
        cmd_layout.setSpacing(6)

        cmd_layout.addWidget(QLabel("Send Command:"))
        cmd_layout.addWidget(cmd_input)
        cmd_layout.addWidget(cmd_btn)

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setFrameShadow(QFrame.Sunken)

        cmd_layout.addWidget(divider)

        cmd_layout.addWidget(QLabel("Quick Commands:"))

        self.quick_buttons_container = QWidget()
        self.quick_buttons_layout = QGridLayout(self.quick_buttons_container)
        self.quick_buttons_layout.setContentsMargins(0, 0, 0, 0)
        self.quick_buttons_layout.setHorizontalSpacing(6)
        self.quick_buttons_layout.setVerticalSpacing(6)

        cmd_layout.addWidget(self.quick_buttons_container)

        cmd_layout.addWidget(QLabel("History:"))

        self.cmd_history = QListWidget()
        cmd_layout.addWidget(self.cmd_history)

        cmd_panel = QWidget()
        cmd_panel.setLayout(cmd_layout)
        cmd_panel.setFixedWidth(300)

        self.cmd_input = cmd_input
        self.cmd_btn = cmd_btn

        main_layout.addWidget(cmd_panel)

        main_layout.setStretchFactor(self.table_viewer, 1)
        main_layout.setStretchFactor(self.non_table_display, 2)

        # Serial worker
        self.worker = SerialWorker(
            port=port,
            baudrate=baudrate,
            data_logger=self.data_logger
        )

        self.thread = QThread()
        self.worker.moveToThread(self.thread)

        self.worker.data_received.connect(self.update_view)
        self.thread.started.connect(self.worker.start)
        self.thread.start()

        self.build_quick_buttons(buttons or [])

    def build_quick_buttons(self, buttons):
        """Create buttons from a list of {'name': ..., 'value': ...} dicts."""
        while self.quick_buttons_layout.count():
            item = self.quick_buttons_layout.takeAt(0)
            w = item.widget()

            if w:
                w.setParent(None)

        if not buttons:
            note = QLabel("No quick commands configured.")
            note.setStyleSheet("color: #777; font-style: italic;")
            self.quick_buttons_layout.addWidget(note, 0, 0)
            return

        cols = 2
        row = 0
        col = 0

        for b in buttons:
            name = b.get("name", "")
            value = b.get("value", "")

            if not name or not value:
                continue

            btn = QPushButton(name)
            btn.clicked.connect(lambda _, v=value: self.send_quick_command(v))

            self.quick_buttons_layout.addWidget(btn, row, col)

            col += 1

            if col >= cols:
                col = 0
                row += 1

    def send_quick_command(self, value):
        """Send quick command. Add to history only if sending succeeds."""
        value = str(value).strip()

        if not value:
            return

        if self.worker.send_command(value):
            self.cmd_history.addItem(value)

    def send_command(self):
        """Send typed command. Add to history only if sending succeeds."""
        cmd = self.cmd_input.text().strip()

        if not cmd:
            return

        if self.worker.send_command(cmd):
            self.cmd_history.addItem(cmd)
            self.cmd_input.clear()

    def update_view(self, data):
        self.table_viewer.display_tables(data)
        self.render_non_table(data)

    def render_non_table(self, data):
        while self.non_table_layout.count():
            w = self.non_table_layout.takeAt(0).widget()

            if w:
                w.setParent(None)

        def add_wrapping_label(text, bold=False, indent=0):
            lbl = QLabel(text)

            if bold:
                lbl.setStyleSheet("font-weight:bold;")

            if indent:
                lbl.setIndent(indent)

            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)

            self.non_table_layout.addWidget(lbl)

        def recurse(d):
            if isinstance(d, dict):
                for k, v in d.items():
                    if isinstance(v, list) and self.table_viewer.is_2d_array(v):
                        continue

                    add_wrapping_label(f"{k}:", bold=True)
                    add_wrapping_label(str(v), indent=10)

            elif isinstance(d, list):
                for item in d:
                    recurse(item)

            else:
                add_wrapping_label(str(d))

        recurse(data)

    def closeEvent(self, event):
        try:
            self.worker.stop()
        except Exception:
            pass

        try:
            self.thread.quit()
            self.thread.wait(1500)
        except Exception:
            pass

        try:
            if self.data_logger:
                self.data_logger.close()
        except Exception:
            pass

        event.accept()


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    cfg = load_config(config_path)

    heatmaps = cfg.get("heatmaps")
    legacy_tables = cfg.get("heatmap_tables")
    legacy_max_dev = cfg.get("max_deviation", 0.05)

    serial_logger = SerialDataLogger(
        log_dir=cfg.get("log_dir", "logs"),
        log_format=cfg.get("log_format", "csv"),
        prefix=cfg.get("log_prefix", "serial_log")
    )

    app = QApplication(sys.argv)

    win = App(
        port=cfg.get("COM", "/dev/ttyUSB0"),
        baudrate=cfg.get("BAUD", 500000),
        buttons=cfg.get("buttons", []),
        precision=cfg.get("precision", 3),
        heatmaps=heatmaps,
        legacy_tables=legacy_tables,
        legacy_max_dev=legacy_max_dev,
        data_logger=serial_logger
    )

    win.show()

    try:
        exit_code = app.exec_()
    finally:
        serial_logger.close()

    sys.exit(exit_code)