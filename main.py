import sys
import json
import serial
try:
    import yaml
except ImportError:
    yaml = None

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QHeaderView, QScrollArea, QLineEdit, QPushButton,
    QListWidget, QSizePolicy, QGridLayout, QFrame
)
from PyQt5.QtWidgets import QAbstractScrollArea
from PyQt5.QtGui import QColor
from PyQt5.QtCore import Qt, QObject, QThread, pyqtSignal


def load_config(path="config.yaml"):
    """Load YAML configuration if available. Returns dict with defaults."""
    cfg = {
        "COM": "/dev/ttyUSB0",
        "BAUD": 500000,
        "buttons": [],
        "precision": 3,
        # Preferred: per-table rules
        "heatmaps": None,          # list of {"name": str, "max_deviation": float}
        # Legacy fallbacks (used only if 'heatmaps' not provided)
        "heatmap_tables": None,    # list[str] or str
        "max_deviation": 0.05
    }
    if not path:
        return cfg
    try:
        if yaml is None:
            print("[WARN] PyYAML not installed; using defaults. Install with: pip install pyyaml")
            return cfg
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if isinstance(data, dict):
            if "COM" in data and isinstance(data["COM"], str) and data["COM"]:
                cfg["COM"] = data["COM"]
            if "BAUD" in data:
                try:
                    cfg["BAUD"] = int(data["BAUD"])
                except Exception:
                    print("[WARN] Invalid BAUD in YAML; using default 500000")
            if "buttons" in data and isinstance(data["buttons"], list):
                # normalize buttons to list of {name, value}
                norm = []
                for item in data["buttons"]:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                    if name and value:
                        norm.append({"name": name, "value": value})
                    else:
                        print(f"[WARN] Skipping button with missing name/value: {item}")
                cfg["buttons"] = norm

            # Precision (accepts "precision" or "PRECISION")
            prec_key = "precision" if "precision" in data else ("PRECISION" if "PRECISION" in data else None)
            if prec_key is not None:
                try:
                    p = int(data[prec_key])
                    if p < 0:
                        raise ValueError
                    cfg["precision"] = p
                except Exception:
                    print("[WARN] Invalid precision in YAML; using default 3")

            # Preferred: 'heatmaps' per-table list
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
                            hm.append({"name": name, "max_deviation": max(0.0, md)})
                cfg["heatmaps"] = hm if hm else None

            # Legacy fallbacks (only if no 'heatmaps' provided)
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
                        print("[WARN] Invalid max_deviation in YAML; using default 0.05")
    except FileNotFoundError:
        # silent fallback to defaults
        pass
    except Exception as e:
        print(f"[WARN] Failed to load YAML config: {e}")
    return cfg


class SerialWorker(QObject):
    data_received = pyqtSignal(dict)

    def __init__(self, port="/dev/ttyUSB0", baudrate=500000):
        super().__init__()
        self.port = port
        self.baudrate = baudrate
        self._running = True
        self.serial_port = None

    def start(self):
        try:
            self.serial_port = serial.Serial(self.port, self.baudrate, timeout=1)
            print(f"[INFO] Opened serial {self.port} @ {self.baudrate}")
        except serial.SerialException as e:
            print(f"[ERROR] Could not open serial port: {e}")
            return

        buffer = ""
        while self._running:
            try:
                line = self.serial_port.readline().decode('utf-8', errors='ignore')
                if not line:
                    continue
                buffer = line
                try:
                    json_data = json.loads(buffer)
                    self.data_received.emit(json_data)
                    buffer = ""
                except json.JSONDecodeError:
                    continue
            except Exception as e:
                print(f"[ERROR] Error reading serial: {e}")

    def stop(self):
        self._running = False
        try:
            if self.serial_port and self.serial_port.is_open:
                self.serial_port.close()
        except Exception:
            pass

    # >>> Same as your provided file <<<
    def send_command(self, cmd):
        if self.serial_port and self.serial_port.is_open:
            self.serial_port.write((cmd + "\n").encode('utf-8'))
            print(f"[INFO] Sent command: {cmd}")


class TableViewer(QWidget):
    def __init__(self, precision=3, heatmap_rules=None, default_max_dev=0.05):
        """
        heatmap_rules: dict[str, float] mapping table name -> max_deviation (fraction).
                       If None, apply to all tables using default_max_dev.
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
                    # Determine if/which rule applies
                    max_dev = None
                    if self.heatmap_rules is None:
                        max_dev = self.default_max_dev  # apply to all
                    elif key in self.heatmap_rules:
                        max_dev = self.heatmap_rules[key]  # apply only if named
                    # Render with optional heatmap
                    self.add_table(value, max_dev=max_dev)
                elif isinstance(value, (dict, list)):
                    self.render_data(value)
        elif isinstance(data, list):
            for item in data:
                self.render_data(item)

    def is_2d_array(self, arr):
        return isinstance(arr, list) and arr and all(isinstance(r, list) for r in arr) and all(len(r) == len(arr[0]) for r in arr)

    def add_label(self, text, bold=False):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if bold:
            label.setStyleSheet("font-weight: bold")
        self.content_layout.addWidget(label)

    def _format_for_display(self, val):
        """Format numeric values with up to `precision` decimals, trimming trailing zeros."""
        try:
            num = float(val)
            s = f"{num:.{self.precision}f}"
            if self.precision > 0:
                s = s.rstrip('0').rstrip('.')
            else:
                s = s.split('.')[0]
            return s
        except Exception:
            return str(val)

    # --------- Color helpers for smooth gradients ----------
    @staticmethod
    def _lerp(a, b, t):
        return int(a + (b - a) * max(0.0, min(1.0, t)))

    @staticmethod
    def _qcolor_from_rgb(r, g, b):
        return QColor(int(r), int(g), int(b))

    def _green_color(self, t):
        """t in [0,1]; 0 = at avg (light), 1 = edge of green band (deeper)."""
        # light green -> medium green (pleasant, readable)
        r = self._lerp(234, 184, t)   # from #EAEAEA? No, pick #EAFBEA -> #B8F0B8
        g = self._lerp(251, 240, t)
        b = self._lerp(234, 184, t)
        return self._qcolor_from_rgb(r, g, b)

    def _red_color(self, t):
        """t in [0,1]; 0 = just outside band (light red), 1 = far away (pleasant stronger red)."""
        r = self._lerp(255, 255, t)   # keep red at 255
        g = self._lerp(234, 140, t)   # #FFEAEA -> #FF8C8C
        b = self._lerp(234, 140, t)
        return self._qcolor_from_rgb(r, g, b)
    # ------------------------------------------------------

    def add_table(self, table_data, max_dev=None):
        rows, cols = len(table_data), len(table_data[0])

        # compute average for heatmap
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

        # Choose a cap so "far" deviations don't all look identical.
        # Full-strength red at ~5x the band width.
        red_cap_factor = 5.0

        for i in range(rows):
            for j in range(cols):
                raw_val = table_data[i][j]
                display_text = self._format_for_display(raw_val)
                item = QTableWidgetItem(display_text)

                if max_dev is not None and avg != 0:
                    try:
                        num = float(raw_val)
                        diff = abs(num - avg) / abs(avg)  # relative deviation
                        if diff <= max_dev:
                            # Green fade: closer to avg -> lighter; at edge -> deeper
                            t = diff / max_dev  # 0..1
                            color = self._green_color(t)
                            item.setBackground(color)
                        else:
                            # Red fade: just outside band -> light red; farther -> stronger red
                            over = diff - max_dev
                            denom = max(max_dev * red_cap_factor, 1e-12)
                            t = max(0.0, min(1.0, over / denom))
                            color = self._red_color(t)
                            item.setBackground(color)
                    except Exception:
                        pass

                table.setItem(i, j, item)

        table.resizeColumnsToContents()
        height = sum(table.rowHeight(i) for i in range(rows)) + table.horizontalHeader().height()
        table.setFixedHeight(height)
        table.setStyleSheet("QTableWidget { border: 1px solid #ccc; }")
        self.content_layout.addWidget(table)

    def clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)


class App(QWidget):
    def __init__(self, port="/dev/ttyUSB0", baudrate=500000, buttons=None, precision=3,
                 heatmaps=None, legacy_tables=None, legacy_max_dev=0.05):
        super().__init__()
        self.setWindowTitle("Serial JSON Monitor")
        self.setMinimumSize(1200, 600)

        # Build heatmap rules dict[name] = max_deviation
        rules = None
        if heatmaps:  # preferred
            rules = {str(h["name"]): float(h.get("max_deviation", legacy_max_dev)) for h in heatmaps if "name" in h}
        elif legacy_tables:  # legacy
            rules = {str(name): float(legacy_max_dev) for name in legacy_tables}

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(5, 5, 5, 5)
        main_layout.setSpacing(5)

        # Table section (RIGHT)
        self.table_viewer = TableViewer(
            precision=precision,
            heatmap_rules=rules,
            default_max_dev=legacy_max_dev
        )
        self.table_viewer.setMinimumWidth(1100)
        self.table_viewer.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        main_layout.addWidget(self.table_viewer)

        # Non-table section (MIDDLE)
        self.non_table_display = QScrollArea()
        self.non_table_display.setWidgetResizable(True)
        self.non_table_display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.non_table_content = QWidget()
        self.non_table_layout = QVBoxLayout(self.non_table_content)
        self.non_table_layout.setContentsMargins(5, 5, 5, 5)
        self.non_table_layout.setSpacing(5)
        self.non_table_display.setWidget(self.non_table_content)
        main_layout.addWidget(self.non_table_display)

        # Command section (LEFT) - fixed width
        cmd_input = QLineEdit()
        cmd_btn = QPushButton("Send")
        cmd_btn.clicked.connect(self.send_command)
        cmd_input.returnPressed.connect(self.send_command)

        self.cmd_history = QListWidget()

        cmd_layout = QVBoxLayout()
        cmd_layout.setContentsMargins(5, 5, 5, 5)
        cmd_layout.setSpacing(6)
        cmd_layout.addWidget(QLabel("Send Command:"))
        cmd_layout.addWidget(cmd_input)
        cmd_layout.addWidget(cmd_btn)

        # Divider
        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setFrameShadow(QFrame.Sunken)
        cmd_layout.addWidget(divider)

        # Quick command buttons from YAML
        cmd_layout.addWidget(QLabel("Quick Commands:"))
        self.quick_buttons_container = QWidget()
        self.quick_buttons_layout = QGridLayout(self.quick_buttons_container)
        self.quick_buttons_layout.setContentsMargins(0, 0, 0, 0)
        self.quick_buttons_layout.setHorizontalSpacing(6)
        self.quick_buttons_layout.setVerticalSpacing(6)
        cmd_layout.addWidget(self.quick_buttons_container)

        # History
        cmd_layout.addWidget(QLabel("History:"))
        self.cmd_history = QListWidget()
        cmd_layout.addWidget(self.cmd_history)

        cmd_panel = QWidget()
        cmd_panel.setLayout(cmd_layout)
        cmd_panel.setFixedWidth(300)
        self.cmd_input = cmd_input
        self.cmd_btn = cmd_btn
        main_layout.addWidget(cmd_panel)

        # Layout stretch
        main_layout.setStretchFactor(self.table_viewer, 1)
        main_layout.setStretchFactor(self.non_table_display, 2)

        # Serial worker
        self.worker = SerialWorker(port, baudrate)
        self.thread = QThread()
        self.worker.moveToThread(self.thread)
        self.worker.data_received.connect(self.update_view)
        self.thread.started.connect(self.worker.start)
        self.thread.start()

        # Build quick command buttons
        self.build_quick_buttons(buttons or [])

    def build_quick_buttons(self, buttons):
        """Create buttons from a list of {'name':..., 'value':...} dicts."""
        # clear previous
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

        # Arrange in a grid (e.g., 2 columns)
        cols = 2
        row = col = 0
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
        """Send the quick command value and add to history."""
        value = str(value).strip()
        if not value:
            return
        # Direct call to worker, same as your original code
        self.worker.send_command(value)
        self.cmd_history.addItem(value)

    def send_command(self):
        cmd = self.cmd_input.text().strip()
        if cmd:
            # Direct call to worker, same as your original code
            self.worker.send_command(cmd)
            self.cmd_history.addItem(cmd)
            self.cmd_input.clear()

    def update_view(self, data):
        self.table_viewer.display_tables(data)
        self.render_non_table(data)

    def render_non_table(self, data):
        # clear
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
        event.accept()


if __name__ == "__main__":
    # Optional: config path from CLI
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    cfg = load_config(config_path)

    # Build args for App from cfg (handle preferred + legacy)
    heatmaps = cfg.get("heatmaps")
    legacy_tables = cfg.get("heatmap_tables")
    legacy_max_dev = cfg.get("max_deviation", 0.05)

    app = QApplication(sys.argv)
    win = App(
        cfg.get("COM", "/dev/ttyUSB0"),
        cfg.get("BAUD", 500000),
        cfg.get("buttons", []),
        cfg.get("precision", 3),
        heatmaps=heatmaps,
        legacy_tables=legacy_tables,
        legacy_max_dev=legacy_max_dev
    )
    win.show()
    sys.exit(app.exec_())

