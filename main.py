import sys  # noqa: I001
import json
import msgpack
import time
import csv
import os
import re
import threading
import logging
import queue
import base64
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

from PyQt5.QtWidgets import (  # noqa: I001
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QHeaderView, QScrollArea, QLineEdit, QPushButton,
    QListWidget, QSizePolicy, QGridLayout, QFrame, QSplitter
)
from PyQt5.QtWidgets import QAbstractScrollArea
from PyQt5.QtGui import QColor
from PyQt5.QtCore import Qt, QObject, QThread, QTimer, pyqtSignal


# ---------------------------------------------------------------------
# Basic program logging
# ---------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger("serial_json_monitor")


# ---------------------------------------------------------------------
# Data logger
# ---------------------------------------------------------------------

class SerialDataLogger:
    """
    CSV mode:
        Keeps the CSV format flattened:
            timestamp_iso,direction,port,path,value_text,value_number,is_numeric,raw_json

    MCAP mode:
        Dynamically creates one topic per received JSON message type.

        Example received JSON:
            {"type": "imu", "ax": 1.2, "ay": 3.4}

        MCAP topic:
            /serial/imu

        MCAP message:
            {"type": "imu", "ax": 1.2, "ay": 3.4}

        Repeated IMU messages continue going to:
            /serial/imu

        Another message:
            {"type": "gps", "lat": 1.0, "lon": 2.0}

        MCAP topic:
            /serial/gps
    """

    def __init__(
        self,
        log_dir="logs",
        log_format="csv",
        prefix="serial_log",
        mcap_topic_prefix="/serial",
        mcap_default_topic="/serial/json",
        mcap_topic_field=None
    ):
        self.log_dir = log_dir or "logs"
        self.log_format = str(log_format or "jsonl").lower().strip()
        self.prefix = prefix or "serial_log"

        self.mcap_topic_prefix = self._clean_topic_prefix(mcap_topic_prefix)
        self.mcap_default_topic = self._clean_topic(mcap_default_topic or "/serial/json")
        self.mcap_topic_field = str(mcap_topic_field).strip() if mcap_topic_field else None

        self.lock = threading.RLock()
        self.closed = False

        os.makedirs(self.log_dir, exist_ok=True)

        start_stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        self.csv_file = None
        self.csv_writer = None

        self.mcap_file = None
        self.mcap_writer = None
        self.jsonl_file = None
        self.invalid_file = None

        # topic -> channel_id
        self.mcap_channels = {}

        # topic -> schema_id
        self.mcap_schemas = {}

        # Active MCAP schema signature per topic. MCAP schemas/channels are
        # immutable after registration, so if a topic's JSON structure changes
        # we rotate to a fresh MCAP file before writing that changed frame.
        self.mcap_schema_signatures = {}
        self._mcap_start_stamp = start_stamp
        self._mcap_part_index = 0
        self.mcap_paths = []

        opened_mcap = False

        if self.log_format == "jsonl":
            self._open_jsonl(start_stamp)
            logger.info("JSONL serial log file: %s", self.path)
        elif self.log_format == "csv":
            self._open_csv(start_stamp)
            logger.info("CSV serial log file: %s", self.path)
        elif self.log_format == "mcap":
            if MCAP_AVAILABLE:
                try:
                    self._open_mcap(start_stamp)
                    logger.info("MCAP serial log file: %s", self.path)
                    opened_mcap = True
                except Exception as e:
                    logger.error("Could not start MCAP logging: %s", e)
                    logger.warning("Falling back to CSV logging.")
            else:
                logger.warning("MCAP package not installed. Falling back to CSV.")
                logger.warning("Install with: pip install mcap")

        if self.log_format == "mcap" and not opened_mcap:
            self.log_format = "csv"
            self._open_csv(start_stamp)
            logger.info("CSV serial log file: %s", self.path)

        # Invalid/corrupted candidates are captured separately, byte-for-byte
        # (base64) so serial corruption can be diagnosed without blocking RX.
        self.invalid_path = os.path.join(
            self.log_dir, f"{self.prefix}_{start_stamp}_invalid.jsonl"
        )
        self.invalid_file = open(self.invalid_path, "w", encoding="utf-8")

        # Logging is intentionally asynchronous. The serial receive thread only
        # enqueues parsed frames; this writer thread persists every queued frame
        # in FIFO order without blocking UART reception. The queue is unbounded
        # on purpose: frames are never dropped merely because disk I/O is slow.
        self._log_queue = queue.Queue()
        self._log_sentinel = object()
        self._flush_interval = 0.25
        self._frames_enqueued = 0
        self._frames_written = 0
        self._writer_error = None
        self._log_thread = threading.Thread(
            target=self._writer_loop,
            name="serial-data-logger",
            daemon=False
        )
        self._log_thread.start()

    # ------------------------------------------------------------
    # File opening
    # ------------------------------------------------------------

    def _open_jsonl(self, start_stamp):
        self.path = os.path.join(self.log_dir, f"{self.prefix}_{start_stamp}.jsonl")
        # Large user-space buffering: one write per frame, no flattening.
        self.jsonl_file = open(self.path, "w", encoding="utf-8", buffering=1024 * 1024)

    def _open_csv(self, start_stamp):
        self.path = os.path.join(self.log_dir, f"{self.prefix}_{start_stamp}.csv")

        self.csv_file = open(self.path, "w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)

        self.csv_writer.writerow([
            "timestamp_iso",
            "direction",
            "port",
            "path",
            "value_text",
            "value_number",
            "is_numeric",
            "raw_json"
        ])

        self.csv_file.flush()

    def _open_mcap(self, start_stamp, part_index=0):
        self._mcap_start_stamp = start_stamp
        self._mcap_part_index = int(part_index)

        if self._mcap_part_index == 0:
            filename = f"{self.prefix}_{start_stamp}.mcap"
        else:
            filename = (
                f"{self.prefix}_{start_stamp}_format_{self._mcap_part_index:03d}.mcap"
            )

        self.path = os.path.join(self.log_dir, filename)

        self.mcap_file = open(self.path, "wb")
        self.mcap_writer = McapWriter(self.mcap_file)

        self.mcap_writer.start(
            profile="jsonschema",
            library="pyqt_serial_json_monitor"
        )

        self.mcap_channels = {}
        self.mcap_schemas = {}
        self.mcap_schema_signatures = {}
        self.mcap_paths.append(self.path)

    def _finish_current_mcap(self):
        """Finish and close the currently active MCAP part, if any."""
        writer = self.mcap_writer
        file_obj = self.mcap_file

        self.mcap_writer = None
        self.mcap_file = None

        if writer is not None:
            try:
                writer.finish()
            except Exception as e:
                logger.warning("Error finishing MCAP part: %s", e)

        if file_obj is not None:
            try:
                file_obj.flush()
            except Exception:
                pass
            try:
                file_obj.close()
            except Exception:
                pass

    def _rotate_mcap_for_schema_change(self, topic, old_signature, new_signature):
        """Start a fresh Foxglove/MCAP file before a changed schema is written."""
        old_path = self.path
        self._finish_current_mcap()

        next_index = self._mcap_part_index + 1
        self._open_mcap(self._mcap_start_stamp, part_index=next_index)

        logger.warning(
            "MCAP schema changed for topic %s; closed %s and started %s",
            topic, old_path, self.path
        )

    # ------------------------------------------------------------
    # MCAP topic and schema helpers
    # ------------------------------------------------------------

    def _clean_topic_prefix(self, prefix):
        prefix = str(prefix or "/serial").strip()

        if not prefix.startswith("/"):
            prefix = "/" + prefix

        prefix = prefix.rstrip("/")

        if not prefix:
            prefix = "/serial"

        return prefix

    def _clean_topic(self, topic):
        topic = str(topic or self.mcap_default_topic).strip()

        if not topic:
            topic = self.mcap_default_topic

        if not topic.startswith("/"):
            topic = "/" + topic

        topic = re.sub(r"[^A-Za-z0-9_/.-]+", "_", topic)
        topic = re.sub(r"/+", "/", topic)

        return topic

    def _topic_from_json(self, data):
        """
        Dynamically choose topic from the JSON.

        Priority:
            1. Configured mcap_topic_field
            2. mcap_topic
            3. topic
            4. type
            5. message_type
            6. name
            7. default topic

        Example:
            {"type": "imu", "ax": 1.2}
            -> /serial/imu
        """
        if not isinstance(data, dict):
            return self.mcap_default_topic

        topic_fields = []

        if self.mcap_topic_field:
            topic_fields.append(self.mcap_topic_field)

        topic_fields.extend([
            "mcap_topic",
            "topic",
            "type",
            "message_type",
            "name"
        ])

        for field in topic_fields:
            if field not in data:
                continue

            value = data.get(field)

            if isinstance(value, (dict, list)):
                continue

            value = str(value).strip()

            if not value:
                continue

            if value.startswith("/"):
                return self._clean_topic(value)

            return self._clean_topic(f"{self.mcap_topic_prefix}/{value}")

        return self.mcap_default_topic

    def _json_type_for_value(self, value):
        """
        Convert Python values to a Foxglove-friendly JSON schema.

        Integers and floats are intentionally normalized to JSON "number" so
        ordinary telemetry changes such as 0 -> 0.25 do not cause needless
        MCAP file rotation.
        """
        if isinstance(value, bool):
            return {"type": "boolean"}

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return {"type": "number"}

        if isinstance(value, str):
            return {"type": "string"}

        if value is None:
            return {"type": "null"}

        if isinstance(value, list):
            if not value:
                return {
                    "type": "array",
                    "items": {}
                }

            # Most telemetry arrays are homogeneous. Use the first item for the
            # schema, matching the original behavior, while the full outer shape
            # is still represented recursively for nested arrays/objects.
            return {
                "type": "array",
                "items": self._json_type_for_value(value[0])
            }

        if isinstance(value, dict):
            properties = {}

            for key, child_value in value.items():
                properties[str(key)] = self._json_type_for_value(child_value)

            return {
                "type": "object",
                "properties": properties,
                "additionalProperties": True
            }

        return {"type": "string"}

    def _mcap_schema_signature(self, topic, data):
        """Return a deterministic signature of the schema, not the values."""
        _name, schema = self._schema_from_json(topic, data)
        schema = dict(schema)
        schema.pop("title", None)
        return json.dumps(schema, sort_keys=True, separators=(",", ":"))

    def _schema_from_json(self, topic, data):
        """
        Build a Foxglove-friendly JSON schema from the first message on a topic.
        """
        schema_name = topic.strip("/").replace("/", "_").replace("-", "_").replace(".", "_")

        schema = self._json_type_for_value(data)

        if schema.get("type") != "object":
            schema = {
                "type": "object",
                "properties": {
                    "value": self._json_type_for_value(data)
                },
                "additionalProperties": True
            }

        schema["title"] = schema_name
        schema["additionalProperties"] = True

        return schema_name, schema

    def _get_mcap_channel(self, topic, data):
        """
        Return a channel compatible with the current JSON structure.

        If an existing topic changes schema, finish the current MCAP file and
        open a new one before registering/writing the changed message. This
        keeps every Foxglove file internally schema-consistent.
        """
        topic = self._clean_topic(topic)
        signature = self._mcap_schema_signature(topic, data)

        old_signature = self.mcap_schema_signatures.get(topic)
        if old_signature is not None and old_signature != signature:
            self._rotate_mcap_for_schema_change(topic, old_signature, signature)
            # Rotation clears all channels/signatures. The changed message will
            # now define the schema in the new file.

        if topic in self.mcap_channels:
            return self.mcap_channels[topic]

        schema_name, schema = self._schema_from_json(topic, data)

        schema_id = self.mcap_writer.register_schema(
            name=schema_name,
            encoding="jsonschema",
            data=json.dumps(schema, separators=(",", ":")).encode("utf-8")
        )

        channel_id = self.mcap_writer.register_channel(
            topic=topic,
            message_encoding="json",
            schema_id=schema_id
        )

        self.mcap_schemas[topic] = schema_id
        self.mcap_channels[topic] = channel_id
        self.mcap_schema_signatures[topic] = signature

        logger.info(
            "Created MCAP topic %s in %s",
            topic, os.path.basename(self.path)
        )

        return channel_id

    def _write_mcap_json(self, topic, payload):
        """
        Write one full JSON payload to its dynamic topic.
        """
        now_ns = time.time_ns()
        channel_id = self._get_mcap_channel(topic, payload)

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        self.mcap_writer.add_message(
            channel_id=channel_id,
            log_time=now_ns,
            publish_time=now_ns,
            data=data
        )


    # ------------------------------------------------------------
    # MCAP payload helpers
    # ------------------------------------------------------------

    def _wrap_nested_arrays_for_foxglove(self, value, inside_array=False):
        """
        MCAP only.

        Foxglove message paths do not handle raw nested arrays well.
        This converts nested Python lists into an object format that can be
        addressed like:

            field.array[:].array[:]

        Examples:

            [[1, 2], [3, 4]]

        becomes:

            {
                "array": [
                    {"array": [1, 2]},
                    {"array": [3, 4]}
                ]
            }

        A normal 1D array that is not nested stays unchanged:

            [1, 2, 3]

        But a 1D array inside another array becomes:

            {"array": [1, 2, 3]}
        """
        if isinstance(value, dict):
            return {
                str(key): self._wrap_nested_arrays_for_foxglove(child_value)
                for key, child_value in value.items()
            }

        if isinstance(value, list):
            contains_list = any(isinstance(item, list) for item in value)

            if inside_array or contains_list:
                return {
                    "array": [
                        self._wrap_nested_arrays_for_foxglove(
                            item,
                            inside_array=isinstance(item, list)
                        )
                        for item in value
                    ]
                }

            return [
                self._wrap_nested_arrays_for_foxglove(item)
                for item in value
            ]

        return value

    def _make_mcap_payload(self, data):
        """
        MCAP only.

        Keeps the original JSON shape except nested arrays are rewritten to
        Foxglove-friendly .array[:].array[:] objects. CSV logging is not
        affected by this.
        """
        return self._wrap_nested_arrays_for_foxglove(data)

    # ------------------------------------------------------------
    # CSV helpers
    # ------------------------------------------------------------

    def _flatten_json(self, data, prefix=""):
        values = []

        if isinstance(data, dict):
            for key, value in data.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                values.extend(self._flatten_json(value, path))

        elif isinstance(data, list):
            for index, value in enumerate(data):
                path = f"{prefix}[{index}]" if prefix else f"[{index}]"
                values.extend(self._flatten_json(value, path))

        else:
            values.append((prefix, data))

        return values

    def _value_parts(self, value):
        value_text = str(value)

        if isinstance(value, bool):
            return value_text, float(int(value)), True

        try:
            value_number = float(value)
            return value_text, value_number, True
        except Exception:
            return value_text, 0.0, False

    def _write_csv_row(
        self,
        timestamp_iso,
        direction,
        port,
        path,
        value_text,
        value_number,
        is_numeric,
        raw_json
    ):
        self.csv_writer.writerow([
            timestamp_iso,
            direction,
            port,
            path,
            value_text,
            value_number,
            is_numeric,
            raw_json
        ])

    # ------------------------------------------------------------
    # Asynchronous logging
    # ------------------------------------------------------------

    def _flush_files(self):
        try:
            if self.jsonl_file:
                self.jsonl_file.flush()
        except Exception as e:
            logger.warning("JSONL flush failed: %s", e)

        try:
            if self.invalid_file:
                self.invalid_file.flush()
        except Exception as e:
            logger.warning("Invalid-frame log flush failed: %s", e)

        try:
            if self.csv_file:
                self.csv_file.flush()
        except Exception as e:
            logger.warning("CSV flush failed: %s", e)

        try:
            if self.mcap_file:
                self.mcap_file.flush()
        except Exception as e:
            logger.warning("MCAP flush failed: %s", e)

    def _write_received_sync(self, timestamp_iso, port, data, raw_json=None):
        if self.log_format == "jsonl":
            if raw_json is None:
                raw_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            # Preserve the exact received JSON text as one physical line.
            self.jsonl_file.write(raw_json.rstrip("\r\n") + "\n")
            self._frames_written += 1
            return

        if self.log_format == "mcap":
            topic = self._topic_from_json(data)
            mcap_payload = self._make_mcap_payload(data)
            self._write_mcap_json(topic, mcap_payload)
            self._frames_written += 1
            return

        if raw_json is None:
            raw_json = json.dumps(data, ensure_ascii=False)

        flattened = self._flatten_json(data)
        if not flattened:
            flattened = [("", "")]

        # Keep the existing flattened CSV format, but avoid duplicating the
        # complete ~5 KB JSON string into every flattened row. The exact raw
        # frame is stored on the first row only; all values are still logged.
        rows = []
        for index, (path, value) in enumerate(flattened):
            value_text, value_number, is_numeric = self._value_parts(value)
            rows.append([
                timestamp_iso,
                "receive",
                port,
                path,
                value_text,
                value_number,
                is_numeric,
                raw_json if index == 0 else ""
            ])

        self.csv_writer.writerows(rows)
        self._frames_written += 1

    def _write_send_sync(self, timestamp_iso, port, command, error=None):
        direction = "send_failed" if error is not None else "send"
        payload = {
            "timestamp_iso": timestamp_iso,
            "direction": direction,
            "port": port,
            "command": command
        }
        if error is not None:
            payload["error"] = str(error)

        if self.log_format == "jsonl":
            # Commands share the same append-only journal as received JSON.
            self.jsonl_file.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            return

        if self.log_format == "mcap":
            topic = "/serial/commands_failed" if error is not None else "/serial/commands"
            self._write_mcap_json(topic, payload)
            return

        raw_json = json.dumps(payload, ensure_ascii=False)
        value_text, value_number, is_numeric = self._value_parts(command)
        self._write_csv_row(
            timestamp_iso, direction, port, "command",
            value_text, value_number, is_numeric, raw_json
        )

    def _writer_loop(self):
        last_flush = time.monotonic()

        while True:
            try:
                item = self._log_queue.get(timeout=self._flush_interval)
            except queue.Empty:
                self._flush_files()
                last_flush = time.monotonic()
                continue

            try:
                if item is self._log_sentinel:
                    self._flush_files()
                    return

                kind = item[0]

                if kind == "receive":
                    _, timestamp_iso, port, data, raw_json = item
                    self._write_received_sync(timestamp_iso, port, data, raw_json)

                elif kind == "send":
                    _, timestamp_iso, port, command = item
                    self._write_send_sync(timestamp_iso, port, command)

                elif kind == "send_failed":
                    _, timestamp_iso, port, command, error = item
                    self._write_send_sync(timestamp_iso, port, command, error=error)

                elif kind == "invalid":
                    _, timestamp_iso, port, raw_bytes, error = item
                    record = {
                        "timestamp_iso": timestamp_iso,
                        "port": port,
                        "error": str(error),
                        "length": len(raw_bytes),
                        "raw_base64": base64.b64encode(raw_bytes).decode("ascii")
                    }
                    self.invalid_file.write(
                        json.dumps(record, separators=(",", ":")) + "\n"
                    )

            except Exception as e:
                self._writer_error = e
                logger.exception("Serial data logger write failed: %s", e)

            finally:
                self._log_queue.task_done()

            now = time.monotonic()
            if now - last_flush >= self._flush_interval:
                self._flush_files()
                last_flush = now

    # ------------------------------------------------------------
    # Public logging methods
    # ------------------------------------------------------------

    def log_received(self, port, data, raw_json=None):
        timestamp_iso = datetime.now().isoformat(timespec="milliseconds")

        with self.lock:
            if self.closed:
                return False

            self._frames_enqueued += 1
            self._log_queue.put(("receive", timestamp_iso, port, data, raw_json))

        return True

    def log_send(self, port, command):
        timestamp_iso = datetime.now().isoformat(timespec="milliseconds")

        with self.lock:
            if self.closed:
                return False
            self._log_queue.put(("send", timestamp_iso, port, command))

        return True

    def log_send_failed(self, port, command, error):
        timestamp_iso = datetime.now().isoformat(timespec="milliseconds")

        with self.lock:
            if self.closed:
                return False
            self._log_queue.put(("send_failed", timestamp_iso, port, command, str(error)))

        return True

    def log_invalid_frame(self, port, raw_bytes, error):
        timestamp_iso = datetime.now().isoformat(timespec="milliseconds")
        raw_bytes = bytes(raw_bytes)
        with self.lock:
            if self.closed:
                return False
            self._log_queue.put(("invalid", timestamp_iso, port, raw_bytes, str(error)))
        return True

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self._log_queue.put(self._log_sentinel)

        # FIFO ordering means the sentinel is processed only after every frame
        # queued before close(). Do not use a short timeout here: closing the
        # application should drain the logger rather than silently lose frames.
        try:
            self._log_thread.join()
        except Exception as e:
            logger.warning("Error waiting for serial logger thread: %s", e)

        if self.log_format == "mcap":
            self._finish_current_mcap()

        self._flush_files()

        try:
            if self.csv_file:
                self.csv_file.close()
        except Exception:
            pass

        logger.info(
            "Serial logger closed: %d/%d received JSON frames written",
            self._frames_written,
            self._frames_enqueued
        )

        if self.log_format == "mcap" and self.mcap_paths:
            logger.info(
                "MCAP output used %d schema-consistent file(s): %s",
                len(self.mcap_paths),
                ", ".join(os.path.basename(p) for p in self.mcap_paths)
            )

        if self._writer_error is not None:
            logger.warning("At least one logger write error occurred: %s", self._writer_error)

# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------

def load_config(path="config.yaml"):
    cfg = {
        "COM": "/dev/ttyUSB0",
        "BAUD": 500000,
        "buttons": [],
        "precision": 3,

        "log_dir": "logs",
        "log_format": "jsonl",
        "log_prefix": "serial_log",

        # MCAP topic settings
        "mcap_topic_prefix": "/serial",
        "mcap_default_topic": "/serial/json",
        "mcap_topic_field": None,

        "heatmaps": None,
        "heatmap_tables": None,
        "max_deviation": 0.05,

        # Strong frame boundary used to recover from missing/spurious newlines.
        # Set to null in YAML to use newline-only framing.
        "frame_start_marker": '{"timestamp":',
        "serialization_format": "json",
        "start_of_text_byte": 0xAA,
        "len_bytes": 2
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

            if fmt in ("jsonl", "csv", "mcap"):
                cfg["log_format"] = fmt
            else:
                logger.warning("Invalid log_format in YAML; using jsonl")

        if "log_prefix" in data and isinstance(data["log_prefix"], str) and data["log_prefix"]:
            cfg["log_prefix"] = data["log_prefix"]

        if "mcap_topic_prefix" in data and isinstance(data["mcap_topic_prefix"], str) and data["mcap_topic_prefix"]:
            cfg["mcap_topic_prefix"] = data["mcap_topic_prefix"]

        if "mcap_default_topic" in data and isinstance(data["mcap_default_topic"], str) and data["mcap_default_topic"]:
            cfg["mcap_default_topic"] = data["mcap_default_topic"]

        if "mcap_topic_field" in data:
            if data["mcap_topic_field"] is None:
                cfg["mcap_topic_field"] = None
            else:
                cfg["mcap_topic_field"] = str(data["mcap_topic_field"]).strip() or None

        if "frame_start_marker" in data:
            marker = data.get("frame_start_marker")
            if marker is None:
                cfg["frame_start_marker"] = None
            else:
                marker = str(marker)
                cfg["frame_start_marker"] = marker if marker else None

        if "serialization_format" in data:
            cfg["serialization_format"] = str(data["serialization_format"]).lower()

        if "start_of_text_byte" in data:
            val = data["start_of_text_byte"]
            if isinstance(val, str):
                cfg["start_of_text_byte"] = int(val, 0)
            else:
                cfg["start_of_text_byte"] = int(val)

        if "len_bytes" in data:
            cfg["len_bytes"] = int(data["len_bytes"])

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

    def __init__(
        self, port="/dev/ttyUSB0", baudrate=500000, data_logger=None,
        frame_start_marker='{"timestamp":', serialization_format="json",
        start_of_text_byte=0xAA, len_bytes=2
    ):
        super().__init__()

        self.port = port
        self.baudrate = baudrate
        self.data_logger = data_logger
        self.serialization_format = str(serialization_format).lower()
        self.start_of_text_byte = start_of_text_byte
        self.len_bytes = len_bytes

        if frame_start_marker is None:
            self.frame_start_marker = None
        elif isinstance(frame_start_marker, bytes):
            self.frame_start_marker = frame_start_marker
        else:
            self.frame_start_marker = str(frame_start_marker).encode("utf-8")

        self._running = True
        self.serial_port = None
        self.reconnect_delay = 1.0
        self._lock = threading.RLock()

    def _close_port(self):
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
        with self._lock:
            self.serial_port = serial.Serial(
                self.port,
                self.baudrate,
                timeout=0.05,
                write_timeout=0.75,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False
            )

            # Supported by pyserial on some platforms (notably Windows).
            # A larger driver RX buffer provides extra margin at 921600 baud.
            try:
                set_buffer_size = getattr(self.serial_port, "set_buffer_size", None)
                if set_buffer_size:
                    set_buffer_size(rx_size=1024 * 1024, tx_size=64 * 1024)
            except Exception as e:
                logger.debug("Could not enlarge serial driver buffers: %s", e)

            logger.info("Opened serial %s @ %s", self.port, self.baudrate)

    def _wait_before_reconnect(self):
        steps = int(self.reconnect_delay * 10)

        for _ in range(max(1, steps)):
            if not self._running:
                return

            time.sleep(0.1)

    def _parse_candidate(self, raw_frame, report_error=True):
        raw_frame = bytes(raw_frame).strip(b" \t\r\n")
        if not raw_frame:
            return False

        try:
            text = raw_frame.decode("utf-8")
            json_data = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            if report_error:
                pos = getattr(e, "pos", -1)
                logger.warning(
                    "Invalid JSON serial frame (%d bytes, error at %s: %s). "
                    "Start=%r End=%r",
                    len(raw_frame), pos, e, raw_frame[:80], raw_frame[-80:]
                )
                if self.data_logger:
                    self.data_logger.log_invalid_frame(self.port, raw_frame, e)
            return False

        if self.data_logger:
            self.data_logger.log_received(self.port, json_data, raw_json=text)
        self.data_received.emit(json_data)
        return True

    @staticmethod
    def _partial_marker_suffix_length(buffer, marker):
        max_len = min(len(buffer), max(0, len(marker) - 1))
        for n in range(max_len, 0, -1):
            if buffer[-n:] == marker[:n]:
                return n
        return 0

    def _drain_marker_frames(self, rx_buffer):
        """
        Frame using the start of the next JSON object, not only newline.

        This is deliberately stronger than newline framing for the telemetry
        stream: if one CR/LF is inserted or lost, the next '{"timestamp":'
        still provides an unambiguous resynchronization point. A complete
        current frame may still be accepted immediately when a newline-delimited
        prefix parses successfully.
        """
        marker = self.frame_start_marker

        while True:
            first = rx_buffer.find(marker)
            if first < 0:
                # Keep only a possible partial marker at the end. Old noise can
                # never become a valid frame and should not grow without bound.
                keep = self._partial_marker_suffix_length(rx_buffer, marker)
                if len(rx_buffer) > max(4096, len(marker) * 4):
                    noise_len = len(rx_buffer) - keep
                    if noise_len > 0:
                        logger.warning(
                            "Discarding %d serial bytes while searching for frame marker %r",
                            noise_len, marker
                        )
                        del rx_buffer[:noise_len]
                return

            if first > 0:
                noise = bytes(rx_buffer[:first])
                if noise.strip(b" \t\r\n"):
                    logger.warning(
                        "Resynchronizing serial stream: discarded %d bytes before %r",
                        first, marker
                    )
                del rx_buffer[:first]

            # A second marker conclusively ends the current candidate, even if
            # the sender lost the newline between frames.
            next_start = rx_buffer.find(marker, len(marker))
            if next_start >= 0:
                candidate = bytes(rx_buffer[:next_start])
                del rx_buffer[:next_start]
                self._parse_candidate(candidate, report_error=True)
                continue

            # No next frame yet. Try every newline currently present. If an
            # early newline is spurious, parsing fails silently and we keep the
            # bytes until a later newline or the next start marker arrives.
            search_from = len(marker)
            while True:
                newline = rx_buffer.find(b"\n", search_from)
                if newline < 0:
                    return
                candidate = bytes(rx_buffer[:newline])
                if self._parse_candidate(candidate, report_error=False):
                    del rx_buffer[:newline + 1]
                    break
                search_from = newline + 1
            # Successfully consumed one complete newline-delimited frame; loop
            # again because more bytes may already be buffered.

    def _parse_msgpack_candidate(self, raw_frame):
        payload = raw_frame[1 + self.len_bytes:]
        try:
            data = msgpack.unpackb(payload, raw=False)
            if not isinstance(data, dict):
                raise ValueError("Parsed msgpack data is not a dictionary")
        except Exception as e:
            logger.warning(
                "Invalid msgpack frame (%d bytes, error: %s).",
                len(raw_frame), e
            )
            if self.data_logger:
                self.data_logger.log_invalid_frame(self.port, raw_frame, e)
            return False

        if self.data_logger:
            self.data_logger.log_received(self.port, data, raw_json=None)
        
        self.data_received.emit(data)
        return True

    def _drain_msgpack_frames(self, rx_buffer):
        while True:
            first = rx_buffer.find(bytes([self.start_of_text_byte]))
            if first < 0:
                rx_buffer.clear()
                return
            
            if first > 0:
                noise = bytes(rx_buffer[:first])
                logger.warning(
                    "Resynchronizing msgpack stream: discarded %d bytes before 0x%02x",
                    first, self.start_of_text_byte
                )
                del rx_buffer[:first]
            
            if len(rx_buffer) < 1 + self.len_bytes:
                return
            
            length_bytes = rx_buffer[1:1 + self.len_bytes]
            payload_len = int.from_bytes(length_bytes, byteorder='little')
            
            total_frame_len = 1 + self.len_bytes + payload_len
            if len(rx_buffer) < total_frame_len:
                return
            
            candidate = bytes(rx_buffer[:total_frame_len])
            del rx_buffer[:total_frame_len]
            self._parse_msgpack_candidate(candidate)

    def _drain_newline_frames(self, rx_buffer):
        while True:
            newline = rx_buffer.find(b"\n")
            if newline < 0:
                return
            candidate = bytes(rx_buffer[:newline])
            del rx_buffer[:newline + 1]
            self._parse_candidate(candidate, report_error=True)

    def start(self):
        rx_buffer = bytearray()
        max_rx_buffer = 1024 * 1024
        bytes_read = 0
        last_stats = time.monotonic()

        while self._running:
            if self.serial_port is None or not self.serial_port.is_open:
                try:
                    self._open_port()
                    rx_buffer.clear()
                except (serial.SerialException, OSError) as e:
                    logger.error("Could not open serial port %s: %s", self.port, e)
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
                    sp = self.serial_port
                    if sp is None or not sp.is_open:
                        continue
                    waiting = sp.in_waiting
                    read_size = min(max(waiting, 1), 65536)
                    chunk = sp.read(read_size)

                if not chunk:
                    continue

                bytes_read += len(chunk)
                rx_buffer.extend(chunk)

                if self.serialization_format == "msgpack":
                    self._drain_msgpack_frames(rx_buffer)
                elif self.frame_start_marker:
                    self._drain_marker_frames(rx_buffer)
                else:
                    self._drain_newline_frames(rx_buffer)

                if len(rx_buffer) > max_rx_buffer:
                    logger.error(
                        "Serial RX buffer exceeded %d bytes; discarding to resynchronize",
                        max_rx_buffer
                    )
                    if self.data_logger:
                        self.data_logger.log_invalid_frame(
                            self.port, bytes(rx_buffer), "RX buffer overflow/resync"
                        )
                    rx_buffer.clear()

                now = time.monotonic()
                if now - last_stats >= 5.0:
                    qsize = self.data_logger._log_queue.qsize() if self.data_logger else 0
                    logger.info(
                        "RX %.1f KiB/s, buffered=%d B, logger_queue=%d",
                        (bytes_read / 1024.0) / (now - last_stats),
                        len(rx_buffer), qsize
                    )
                    bytes_read = 0
                    last_stats = now

            except serial.SerialException as e:
                logger.error("Serial port read error: %s", e)
                self._close_port()
                self._wait_before_reconnect()
            except OSError as e:
                logger.error("Serial OS read error: %s", e)
                self._close_port()
                self._wait_before_reconnect()
            except Exception as e:
                logger.exception("Unexpected serial read error: %s", e)
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
        self._table_widgets = {}
        self._structure_signature = None

    def is_2d_array(self, arr):
        return (
            isinstance(arr, list)
            and arr
            and all(isinstance(r, list) for r in arr)
            and all(len(r) == len(arr[0]) for r in arr)
        )

    def _collect_tables(self, data, prefix=""):
        found = []

        if isinstance(data, dict):
            for key, value in data.items():
                path = f"{prefix}.{key}" if prefix else str(key)

                if isinstance(value, list) and self.is_2d_array(value):
                    found.append((path, str(key), value))
                elif isinstance(value, (dict, list)):
                    found.extend(self._collect_tables(value, path))

        elif isinstance(data, list):
            for index, value in enumerate(data):
                path = f"{prefix}[{index}]" if prefix else f"[{index}]"
                found.extend(self._collect_tables(value, path))

        return found

    def _max_deviation_for(self, name):
        if self.heatmap_rules is None:
            return self.default_max_dev
        return self.heatmap_rules.get(name)

    def display_tables(self, data):
        found = self._collect_tables(data)
        signature = tuple(
            (path, len(values), len(values[0]) if values else 0)
            for path, _name, values in found
        )

        self.content.setUpdatesEnabled(False)
        try:
            if signature != self._structure_signature:
                self.clear_layout(self.content_layout)
                self._table_widgets.clear()

                for path, name, values in found:
                    label = QLabel(name + ":")
                    label.setWordWrap(True)
                    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
                    label.setStyleSheet("font-weight: bold")
                    self.content_layout.addWidget(label)

                    table = self._create_table(values)
                    self.content_layout.addWidget(table)
                    self._table_widgets[path] = (
                        table, self._max_deviation_for(name)
                    )

                self._structure_signature = signature

            for path, _name, values in found:
                table, max_dev = self._table_widgets[path]
                self._update_table(table, values, max_dev=max_dev)

        finally:
            self.content.setUpdatesEnabled(True)
            self.content.update()

    def _create_table(self, table_data):
        rows, cols = len(table_data), len(table_data[0])
        table = QTableWidget(rows, cols)
        table.setVerticalHeaderLabels([str(i + 1) for i in range(rows)])
        table.verticalHeader().setVisible(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.setSizeAdjustPolicy(QAbstractScrollArea.AdjustToContents)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setFocusPolicy(Qt.NoFocus)
        table.setStyleSheet("QTableWidget { border: 1px solid #ccc; }")

        for i in range(rows):
            for j in range(cols):
                table.setItem(i, j, QTableWidgetItem(""))

        self._update_table(table, table_data, max_dev=None)
        table.resizeColumnsToContents()

        # Resize once, then keep column geometry stable. ResizeToContents on a
        # live table causes costly width recalculation after almost every setText.
        header = table.horizontalHeader()
        widths = [table.columnWidth(i) for i in range(cols)]
        header.setSectionResizeMode(QHeaderView.Fixed)
        for i, width in enumerate(widths):
            table.setColumnWidth(i, width)

        height = (
            sum(table.rowHeight(i) for i in range(rows))
            + table.horizontalHeader().height()
        )
        table.setFixedHeight(height)
        return table

    def _format_for_display(self, val):
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
        return self._qcolor_from_rgb(
            self._lerp(234, 184, t),
            self._lerp(251, 240, t),
            self._lerp(234, 184, t)
        )

    def _red_color(self, t):
        return self._qcolor_from_rgb(
            self._lerp(255, 255, t),
            self._lerp(234, 140, t),
            self._lerp(234, 140, t)
        )

    def _violet_color(self, t):
        return self._qcolor_from_rgb(
            self._lerp(230, 173, t),
            self._lerp(245, 216, t),
            self._lerp(255, 255, t)
        )

    def _update_table(self, table, table_data, max_dev=None):
        nums = []
        for row in table_data:
            for value in row:
                try:
                    nums.append(float(value))
                except Exception:
                    pass

        avg = sum(nums) / len(nums) if nums else 0.0
        red_cap_factor = 5.0

        for i, row in enumerate(table_data):
            for j, raw_val in enumerate(row):
                item = table.item(i, j)
                if item is None:
                    item = QTableWidgetItem()
                    table.setItem(i, j, item)

                display_text = self._format_for_display(raw_val)
                if item.text() != display_text:
                    item.setText(display_text)

                # Calculate the desired heatmap color, but only touch Qt's
                # background role when the resulting color actually changes.
                # This avoids hundreds of redundant repaints per frame.
                background_key = None
                background_color = None

                if max_dev is not None and max_dev > 0 and avg != 0:
                    try:
                        num = float(raw_val)
                        relative = (num - avg) / abs(avg)
                        diff_abs = abs(relative)

                        if diff_abs <= max_dev:
                            t = 1.0 - (diff_abs / max_dev)
                            background_color = self._green_color(t)
                        else:
                            over = diff_abs - max_dev
                            denom = max(max_dev * red_cap_factor, 1e-12)
                            t = max(0.0, min(1.0, over / denom))
                            if relative > 0:
                                background_color = self._red_color(t)
                            else:
                                background_color = self._violet_color(t)

                        background_key = (
                            background_color.red(),
                            background_color.green(),
                            background_color.blue()
                        )
                    except Exception:
                        background_key = None
                        background_color = None

                old_key = item.data(Qt.UserRole)
                if old_key != background_key:
                    item.setData(Qt.UserRole, background_key)
                    if background_color is None:
                        item.setData(Qt.BackgroundRole, None)
                    else:
                        item.setBackground(background_color)

    def clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()


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
        data_logger=None,
        frame_start_marker='{"timestamp":',
        serialization_format="json",
        start_of_text_byte=0xAA,
        len_bytes=2
    ):
        super().__init__()

        self.setWindowTitle("Serial JSON Monitor")
        self.setMinimumSize(1200, 600)

        self.data_logger = data_logger
        self.latest_data = None
        self._refresh_pending = False
        self._non_table_keys = None
        self._non_table_widgets = []

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
        main_layout.setSpacing(0)

        # Draggable horizontal splitter for the three main columns.
        # Drag either separator to resize the neighboring columns.
        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setHandleWidth(7)
        main_layout.addWidget(self.main_splitter)

        self.table_viewer = TableViewer(
            precision=precision,
            heatmap_rules=rules,
            default_max_dev=legacy_max_dev
        )

        self.table_viewer.setMinimumWidth(200)
        self.table_viewer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.main_splitter.addWidget(self.table_viewer)

        self.non_table_display = QScrollArea()
        self.non_table_display.setWidgetResizable(True)
        self.non_table_display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.non_table_content = QWidget()
        self.non_table_layout = QVBoxLayout(self.non_table_content)
        self.non_table_layout.setContentsMargins(5, 5, 5, 5)
        self.non_table_layout.setSpacing(5)

        self.non_table_display.setWidget(self.non_table_content)

        self.non_table_display.setMinimumWidth(200)
        self.main_splitter.addWidget(self.non_table_display)

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
        cmd_panel.setMinimumWidth(180)
        cmd_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.cmd_input = cmd_input
        self.cmd_btn = cmd_btn

        self.main_splitter.addWidget(cmd_panel)

        # Initial proportions. The user can freely change them by dragging.
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 2)
        self.main_splitter.setStretchFactor(2, 1)
        self.main_splitter.setSizes([600, 400, 250])

        self.worker = SerialWorker(
            port=port,
            baudrate=baudrate,
            data_logger=self.data_logger,
            frame_start_marker=frame_start_marker,
            serialization_format=serialization_format,
            start_of_text_byte=start_of_text_byte,
            len_bytes=len_bytes
        )

        self.thread = QThread()
        self.worker.moveToThread(self.thread)

        self.worker.data_received.connect(self.update_view)
        self.thread.started.connect(self.worker.start)
        self.thread.start()

        # No fixed GUI refresh rate. Incoming data schedules one zero-delay
        # refresh through the Qt event loop. Bursts are coalesced to the newest
        # frame so serial/logging stay lossless while the GUI remains interactive.
        self.build_quick_buttons(buttons or [])

    def build_quick_buttons(self, buttons):
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
        value = str(value).strip()

        if not value:
            return

        if self.worker.send_command(value):
            self.cmd_history.addItem(value)

    def send_command(self):
        cmd = self.cmd_input.text().strip()

        if not cmd:
            return

        if self.worker.send_command(cmd):
            self.cmd_history.addItem(cmd)
            self.cmd_input.clear()

    def update_view(self, data):
        # Keep the newest display sample. Logging is handled independently in the
        # serial/logger threads and still receives every valid JSON frame.
        self.latest_data = data

        # Schedule at most one refresh. QTimer.singleShot(0, ...) lets Qt process
        # mouse/keyboard/scroll/paint events between redraws instead of imposing
        # a fixed 10 Hz gate or creating an unbounded redraw queue.
        if not self._refresh_pending:
            self._refresh_pending = True
            QTimer.singleShot(0, self.refresh_view)

    def refresh_view(self):
        self._refresh_pending = False

        if self.latest_data is None:
            return

        data = self.latest_data
        self.latest_data = None

        # Child widgets already suppress their own repaint while values change.
        # Avoid disabling updates for the entire top-level window because that
        # can make splitter dragging, scrolling and button feedback feel sticky.
        self.table_viewer.display_tables(data)
        self.render_non_table(data)

    def render_non_table(self, data):
        entries = []

        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, list) and self.table_viewer.is_2d_array(value):
                    continue
                entries.append((str(key), str(value)))
        elif isinstance(data, list):
            entries.append(("value", str(data)))
        else:
            entries.append(("value", str(data)))

        keys = tuple(key for key, _value in entries)

        self.non_table_content.setUpdatesEnabled(False)
        try:
            if keys != self._non_table_keys:
                while self.non_table_layout.count():
                    item = self.non_table_layout.takeAt(0)
                    widget = item.widget()
                    if widget:
                        widget.deleteLater()

                self._non_table_widgets = []

                for key, _value in entries:
                    key_label = QLabel(key + ":")
                    key_label.setStyleSheet("font-weight:bold;")
                    key_label.setWordWrap(True)
                    key_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
                    key_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)

                    value_label = QLabel("")
                    value_label.setIndent(10)
                    value_label.setWordWrap(True)
                    value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
                    value_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)

                    self.non_table_layout.addWidget(key_label)
                    self.non_table_layout.addWidget(value_label)
                    self._non_table_widgets.append(value_label)

                self._non_table_keys = keys

            for value_label, (_key, value) in zip(self._non_table_widgets, entries):
                if value_label.text() != value:
                    value_label.setText(value)

        finally:
            self.non_table_content.setUpdatesEnabled(True)
            self.non_table_content.update()

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
        log_format=cfg.get("log_format", "jsonl"),
        prefix=cfg.get("log_prefix", "serial_log"),
        mcap_topic_prefix=cfg.get("mcap_topic_prefix", "/serial"),
        mcap_default_topic=cfg.get("mcap_default_topic", "/serial/json"),
        mcap_topic_field=cfg.get("mcap_topic_field", None)
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
        data_logger=serial_logger,
        frame_start_marker=cfg.get("frame_start_marker", '{"timestamp":'),
        serialization_format=cfg.get("serialization_format", "json"),
        start_of_text_byte=cfg.get("start_of_text_byte", 0xAA),
        len_bytes=cfg.get("len_bytes", 2)
    )

    win.show()

    try:
        exit_code = app.exec_()
    finally:
        serial_logger.close()

    sys.exit(exit_code)