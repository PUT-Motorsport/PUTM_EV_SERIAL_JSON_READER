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
    cfg = {"COM": "/dev/ttyUSB0", "BAUD": 500000, "buttons": []}
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
    def __init__(self):
        super().__init__()
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
                    self.add_table(value)
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

    def add_table(self, table_data):
        rows, cols = len(table_data), len(table_data[0])

        # compute heatmap thresholds
        nums = []
        for r in table_data:
            for v in r:
                try:
                    nums.append(float(v))
                except Exception:
                    pass
        avg = sum(nums) / len(nums) if nums else 0

        table = QTableWidget(rows, cols)
        table.setVerticalHeaderLabels([str(i + 1) for i in range(rows)])
        table.verticalHeader().setVisible(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.setSizeAdjustPolicy(QAbstractScrollArea.AdjustToContents)

        for i in range(rows):
            for j in range(cols):
                val = table_data[i][j]
                item = QTableWidgetItem(str(val))
                # heatmap
                try:
                    num = float(val)
                    diff = abs(num - avg) / avg * 100 if avg else 0
                    if diff <= 5:
                        color = QColor(204, 255, 204)  # greenish
                    elif diff <= 10:
                        t = (diff - 5) / 5
                        c = int(204 + (255 - 204) * t)
                        color = QColor(c, 255, c)
                    elif diff <= 50:
                        # ramp toward red
                        t = (diff - 10) / 40
                        r = 255
                        g = int(255 * (1 - t) + 204 * t)
                        b = int(255 * (1 - t) + 204 * t)
                        color = QColor(r, g, b)
                    else:
                        color = QColor(255, 204, 204)
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
    def __init__(self, port="/dev/ttyUSB0", baudrate=500000, buttons=None):
        super().__init__()
        self.setWindowTitle("Serial JSON Monitor")
        self.setMinimumSize(1200, 600)

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(5, 5, 5, 5)
        main_layout.setSpacing(5)

        # Table section (RIGHT)
        self.table_viewer = TableViewer()
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

    app = QApplication(sys.argv)
    win = App(cfg.get("COM", "/dev/ttyUSB0"),
              cfg.get("BAUD", 500000),
              cfg.get("buttons", []))
    win.show()
    sys.exit(app.exec_())
