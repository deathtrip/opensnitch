import threading
import sys
import time
import os
import pwd
import json
import ipaddress

from PyQt5 import QtCore, QtGui, uic, QtWidgets

from slugify import slugify

from desktop_parser import LinuxDesktopParser
from config import Config
from version import version

import ui_pb2

DIALOG_UI_PATH = "%s/../res/prompt.ui" % os.path.dirname(sys.modules[__name__].__file__)
class PromptDialog(QtWidgets.QDialog, uic.loadUiType(DIALOG_UI_PATH)[0]):
    _prompt_trigger = QtCore.pyqtSignal()
    _tick_trigger = QtCore.pyqtSignal()
    _timeout_trigger = QtCore.pyqtSignal()

    DEFAULT_TIMEOUT = 15

    ACTION_ALLOW = "allow"
    ACTION_DENY  = "deny"

    FIELD_REGEX_HOST    = "regex_host"
    FIELD_REGEX_IP      = "regex_ip"
    FIELD_PROC_PATH     = "process_path"
    FIELD_PROC_ARGS     = "process_args"
    FIELD_USER_ID       = "user_id"
    FIELD_DST_IP        = "dst_ip"
    FIELD_DST_PORT      = "dst_port"
    FIELD_DST_NETWORK   = "dst_network"
    FIELD_DST_HOST      = "simple_host"

    DURATION_once   = "once"
    DURATION_30s    = "30s"
    DURATION_5m     = "5m"
    DURATION_15m    = "15m"
    DURATION_30m    = "30m"
    DURATION_1h     = "1h"
    # label displayed in the pop-up combo
    DURATION_session = "for this session"
    # field of a rule
    DURATION_restart = "until restart"
    # label displayed in the pop-up combo
    DURATION_forever = "forever"
    # field of a rule
    DURATION_always  = "always"

    CFG_DEFAULT_TIMEOUT = "global/default_timeout"
    CFG_DEFAULT_ACTION = "global/default_action"

    def __init__(self, parent=None):
        QtWidgets.QDialog.__init__(self, parent, QtCore.Qt.WindowStaysOnTopHint)
        # Other interesting flags: QtCore.Qt.Tool | QtCore.Qt.BypassWindowManagerHint
        self._cfg = Config.get()
        self.setupUi(self)

        dialog_geometry = self._cfg.getSettings("promptDialog/geometry")
        if dialog_geometry == QtCore.QByteArray:
            self.restoreGeometry(dialog_geometry)

        self.setWindowTitle("OpenSnitch v%s" % version)

        self._lock = threading.Lock()
        self._con = None
        self._rule = None
        self._local = True
        self._peer = None
        self._prompt_trigger.connect(self.on_connection_prompt_triggered)
        self._timeout_trigger.connect(self.on_timeout_triggered)
        self._tick_trigger.connect(self.on_tick_triggered)
        self._tick = int(self._cfg.getSettings(self.CFG_DEFAULT_TIMEOUT)) if self._cfg.hasKey(self.CFG_DEFAULT_TIMEOUT) else self.DEFAULT_TIMEOUT
        self._tick_thread = None
        self._done = threading.Event()
        self._timeout_text = ""
        self._timeout_triggered = False

        self._apps_parser = LinuxDesktopParser()

        self.denyButton.clicked.connect(self._on_deny_clicked)
        # also accept button
        self.applyButton.clicked.connect(self._on_apply_clicked)
        self._apply_text = "Allow"
        self._deny_text = "Deny"
        self._default_action = self._cfg.getSettings(self.CFG_DEFAULT_ACTION)

        self.whatIPCombo.setVisible(False)
        self.checkDstIP.setVisible(False)
        self.checkDstPort.setVisible(False)
        self.checkUserID.setVisible(False)

        self._ischeckAdvanceded = False
        self.checkAdvanced.toggled.connect(self._checkbox_toggled)

        if QtGui.QIcon.hasThemeIcon("emblem-default") == False:
            self.applyButton.setIcon(self.style().standardIcon(getattr(QtWidgets.QStyle, "SP_DialogApplyButton")))
            self.denyButton.setIcon(self.style().standardIcon(getattr(QtWidgets.QStyle, "SP_DialogCancelButton")))

    def showEvent(self, event):
        super(PromptDialog, self).showEvent(event)
        self.resize(540, 300)
        self.activateWindow()

    def _checkbox_toggled(self, state):
        self.applyButton.setText("%s" % self._apply_text)
        self.denyButton.setText("%s" % self._deny_text)
        self._tick_thread.stop = state

        self.checkDstIP.setVisible(state)
        self.whatIPCombo.setVisible(state)
        self.destIPLabel.setVisible(not state)
        self.checkDstPort.setVisible(state)
        self.checkUserID.setVisible(state)
        self._ischeckAdvanceded = state

    def _set_elide_text(self, widget, text, max_size=132):
        if len(text) > max_size:
            text = text[:max_size] + "..."

        widget.setText(text)

    def promptUser(self, connection, is_local, peer):
        # one at a time
        with self._lock:
            # reset state
            if self._tick_thread != None and self._tick_thread.is_alive():
                self._tick_thread.join()
            self._cfg.reload()
            self._tick = int(self._cfg.getSettings(self.CFG_DEFAULT_TIMEOUT)) if self._cfg.hasKey(self.CFG_DEFAULT_TIMEOUT) else self.DEFAULT_TIMEOUT
            self._tick_thread = threading.Thread(target=self._timeout_worker)
            self._tick_thread.stop = self._ischeckAdvanceded
            self._timeout_triggered = False
            self._rule = None
            self._local = is_local
            self._peer = peer
            self._con = connection
            self._done.clear()
            # trigger and show dialog
            self._prompt_trigger.emit()
            # start timeout thread
            self._tick_thread.start()
            # wait for user choice or timeout
            self._done.wait()

            return self._rule, self._timeout_triggered

    def _timeout_worker(self):
        if self._tick == 0:
            self._timeout_trigger.emit()
            return

        while self._tick > 0 and self._done.is_set() is False:
            t = threading.currentThread()
            # stop only stops the coundtdown, not the thread itself.
            if getattr(t, "stop", True):
                self._tick = int(self._cfg.getSettings(self.CFG_DEFAULT_TIMEOUT))
                time.sleep(1)
                continue

            self._tick -= 1
            self._tick_trigger.emit()
            time.sleep(1)

        if not self._done.is_set():
            self._timeout_trigger.emit()

    @QtCore.pyqtSlot()
    def on_connection_prompt_triggered(self):
        self._render_connection(self._con)
        if self._tick > 0:
            self.show()

    @QtCore.pyqtSlot()
    def on_tick_triggered(self):
        if self._cfg.getSettings(self.CFG_DEFAULT_ACTION) == self.ACTION_ALLOW:
            self._timeout_text = "%s (%d)" % (self._apply_text, self._tick)
            self.applyButton.setText(self._timeout_text)
        else:
            self._timeout_text = "%s (%d)" % (self._deny_text, self._tick)
            self.denyButton.setText(self._timeout_text)

    @QtCore.pyqtSlot()
    def on_timeout_triggered(self):
        self._timeout_triggered = True
        self._send_rule()

    def _configure_default_duration(self):
        if self._cfg.getSettings("global/default_duration") == self.DURATION_once:
            self.durationCombo.setCurrentIndex(0)
        elif self._cfg.getSettings("global/default_duration") == self.DURATION_30s:
            self.durationCombo.setCurrentIndex(1)
        elif self._cfg.getSettings("global/default_duration") == self.DURATION_5m:
            self.durationCombo.setCurrentIndex(2)
        elif self._cfg.getSettings("global/default_duration") == self.DURATION_15m:
            self.durationCombo.setCurrentIndex(3)
        elif self._cfg.getSettings("global/default_duration") == self.DURATION_30m:
            self.durationCombo.setCurrentIndex(4)
        elif self._cfg.getSettings("global/default_duration") == self.DURATION_1h:
            self.durationCombo.setCurrentIndex(5)
        elif self._cfg.getSettings("global/default_duration") == self.DURATION_session:
            self.durationCombo.setCurrentIndex(6)
        elif self._cfg.getSettings("global/default_duration") == self.DURATION_forever:
            self.durationCombo.setCurrentIndex(7)
        else:
            # default to "for this session"
            self.durationCombo.setCurrentIndex(6)

    def _set_cmd_action_text(self):
        if self._cfg.getSettings(self.CFG_DEFAULT_ACTION) == self.ACTION_ALLOW:
            self.applyButton.setText("%s (%d)" % (self._apply_text, self._tick))
            self.denyButton.setText(self._deny_text)
        else:
            self.denyButton.setText("%s (%d)" % (self._deny_text, self._tick))
            self.applyButton.setText(self._apply_text)
        self.checkAdvanced.setFocus()

    def _render_connection(self, con):
        app_name, app_icon, _ = self._apps_parser.get_info_by_path(con.process_path, "terminal")
        if app_name != con.process_path and len(con.process_args) > 1 and con.process_path not in con.process_args:
            self.appPathLabel.setToolTip("Process path: %s" % con.process_path)
            self._set_elide_text(self.appPathLabel, "(%s)" % con.process_path)
        else:
            self.appPathLabel.setFixedHeight(1)
            self.appPathLabel.setText("")

        if app_name == "":
            app_name = "Unknown process"
            self.appNameLabel.setText("Outgoing connection")
        else:
            self.appNameLabel.setText(app_name)
            self.appNameLabel.setToolTip(app_name)

        self.cwdLabel.setToolTip("Process launched from: %s" % con.process_cwd)
        self._set_elide_text(self.cwdLabel, con.process_cwd, max_size=32)

        icon = QtGui.QIcon().fromTheme(app_icon)
        pixmap = icon.pixmap(icon.actualSize(QtCore.QSize(48, 48)))
        self.iconLabel.setPixmap(pixmap)

        if self._local:
            message = "<b>%s</b> is connecting to <b>%s</b> on %s port %d" % ( \
                        app_name,
                        con.dst_host or con.dst_ip,
                        con.protocol,
                        con.dst_port )
        else:
            message = "<b>Remote</b> process <b>%s</b> running on <b>%s</b> is connecting to <b>%s</b> on %s port %d" % ( \
                        app_name,
                        self._peer.split(':')[1],
                        con.dst_host or con.dst_ip,
                        con.protocol,
                        con.dst_port )

        self.messageLabel.setText(message)
        self.messageLabel.setToolTip(message)

        self.sourceIPLabel.setText(con.src_ip)
        self.destIPLabel.setText(con.dst_ip)
        self.destPortLabel.setText(str(con.dst_port))

        if self._local:
            try:
                uid = "%d (%s)" % (con.user_id, pwd.getpwuid(con.user_id).pw_name)
            except:
                uid = ""
        else:
            uid = "%d" % con.user_id

        self.uidLabel.setText(uid)
        self.pidLabel.setText("%s" % con.process_id)
        self._set_elide_text(self.argsLabel, ' '.join(con.process_args))
        self.argsLabel.setToolTip(' '.join(con.process_args))

        self.whatCombo.clear()
        self.whatIPCombo.clear()
        if int(con.process_id) > 0:
            self.whatCombo.addItem("from this executable", self.FIELD_PROC_PATH)

        self.whatCombo.addItem("from this command line", self.FIELD_PROC_ARGS)
        if self.argsLabel.text() == "":
            self._set_elide_text(self.argsLabel, con.process_path)

        # the order of the entries must match those in the preferences dialog
        # prefs -> UI -> Default target
        self.whatCombo.addItem("to port %d" % con.dst_port, self.FIELD_DST_PORT)
        self.whatCombo.addItem("to %s" % con.dst_ip, self.FIELD_DST_IP)
        if int(con.user_id) >= 0:
            self.whatCombo.addItem("from user %s" % uid, self.FIELD_USER_ID)

        self._add_dst_networks_to_combo(self.whatCombo, con.dst_ip)

        if con.dst_host != "" and con.dst_host != con.dst_ip:
            self._add_dsthost_to_combo(con.dst_host)

        self.whatIPCombo.addItem("to %s" % con.dst_ip, self.FIELD_DST_IP)

        parts = con.dst_ip.split('.')
        nparts = len(parts)
        for i in range(1, nparts):
            self.whatCombo.addItem("to %s.*" % '.'.join(parts[:i]), self.FIELD_REGEX_IP)
            self.whatIPCombo.addItem("to %s.*" % '.'.join(parts[:i]), self.FIELD_REGEX_IP)

        self._add_dst_networks_to_combo(self.whatIPCombo, con.dst_ip)

        self._default_action = self._cfg.getSettings(self.CFG_DEFAULT_ACTION)

        self._configure_default_duration()

        if int(con.process_id) > 0:
            self.whatCombo.setCurrentIndex(int(self._cfg.getSettings("global/default_target")))
        else:
            self.whatCombo.setCurrentIndex(2)

        self._set_cmd_action_text()

        self.setFixedSize(self.size())

    # https://gis.stackexchange.com/questions/86398/how-to-disable-the-escape-key-for-a-dialog
    def keyPressEvent(self, event):
        if not event.key() == QtCore.Qt.Key_Escape:
            super(PromptDialog, self).keyPressEvent(event)

    # prevent a click on the window's x
    # from quitting the whole application
    def closeEvent(self, e):
        self._send_rule()
        e.ignore()

    def _add_dst_networks_to_combo(self, combo, dst_ip):
        if type(ipaddress.ip_address(dst_ip)) == ipaddress.IPv4Address:
            combo.addItem("to %s" % ipaddress.ip_network(dst_ip + "/24", strict=False),  self.FIELD_DST_NETWORK)
            combo.addItem("to %s" % ipaddress.ip_network(dst_ip + "/16", strict=False),  self.FIELD_DST_NETWORK)
            combo.addItem("to %s" % ipaddress.ip_network(dst_ip + "/8", strict=False),   self.FIELD_DST_NETWORK)
        else:
            combo.addItem("to %s" % ipaddress.ip_network(dst_ip + "/64", strict=False),  self.FIELD_DST_NETWORK)
            combo.addItem("to %s" % ipaddress.ip_network(dst_ip + "/128", strict=False), self.FIELD_DST_NETWORK)

    def _add_dsthost_to_combo(self, dst_host):
        self.whatCombo.addItem("%s" % dst_host, self.FIELD_DST_HOST)
        self.whatIPCombo.addItem("%s" % dst_host, self.FIELD_DST_HOST)

        parts = dst_host.split('.')[1:]
        nparts = len(parts)
        for i in range(0, nparts - 1):
            self.whatCombo.addItem("to *.%s" % '.'.join(parts[i:]), self.FIELD_REGEX_HOST)
            self.whatIPCombo.addItem("to *.%s" % '.'.join(parts[i:]), self.FIELD_REGEX_HOST)

        if nparts == 1:
            self.whatCombo.addItem("to *%s" % dst_host, self.FIELD_REGEX_HOST)
            self.whatIPCombo.addItem("to *%s" % dst_host, self.FIELD_REGEX_HOST)

    def _get_duration(self, duration_idx):
        if duration_idx == 0:
            return self.DURATION_once
        elif duration_idx == 1:
            return self.DURATION_30s
        elif duration_idx == 2:
            return self.DURATION_5m
        elif duration_idx == 3:
            return self.DURATION_15m
        elif duration_idx == 4:
            return self.DURATION_30m
        elif duration_idx == 5:
            return self.DURATION_1h
        elif duration_idx == 6:
            return self.DURATION_restart
        else:
            return self.DURATION_always

    def _get_combo_operator(self, combo, what_idx):
        if combo.itemData(what_idx) == self.FIELD_PROC_PATH:
            return "simple", "process.path", self._con.process_path

        elif combo.itemData(what_idx) == self.FIELD_PROC_ARGS:
            return "simple", "process.command", ' '.join(self._con.process_args)

        elif combo.itemData(what_idx) == self.FIELD_USER_ID:
            return "simple", "user.id", "%s" % self._con.user_id

        elif combo.itemData(what_idx) == self.FIELD_DST_PORT:
            return "simple", "dest.port", "%s" % self._con.dst_port

        elif combo.itemData(what_idx) == self.FIELD_DST_IP:
            return "simple", "dest.ip", self._con.dst_ip

        elif combo.itemData(what_idx) == self.FIELD_DST_HOST:
            return "simple", "dest.host", combo.currentText()

        elif combo.itemData(what_idx) == self.FIELD_DST_NETWORK:
            # strip "to ": "to x.x.x/20" -> "x.x.x/20"
            return "network", "dest.network", combo.currentText()[3:]

        elif combo.itemData(what_idx) == self.FIELD_REGEX_HOST:
            return "regexp", "dest.host", "%s" % '\.'.join(combo.currentText().split('.')).replace("*", ".*")[3:]

        elif combo.itemData(what_idx) == self.FIELD_REGEX_IP:
            return "regexp", "dest.ip", "%s" % '\.'.join(combo.currentText().split('.')).replace("*", ".*")[3:]

    def _on_deny_clicked(self):
        self._default_action = self.ACTION_DENY
        self._send_rule()

    def _on_apply_clicked(self):
        self._default_action = self.ACTION_ALLOW
        self._send_rule()

    def _get_rule_name(self, rule):
        rule_temp_name = slugify("%s %s" % (rule.action, rule.duration))
        if self._ischeckAdvanceded:
            rule_temp_name = "%s-list" % rule_temp_name
        else:
            rule_temp_name = "%s-simple" % rule_temp_name
        rule_temp_name = slugify("%s %s" % (rule_temp_name, rule.operator.data))

        return rule_temp_name[:128]

    def _send_rule(self):
        self._cfg.setSettings("promptDialog/geometry", self.saveGeometry())
        self._rule = ui_pb2.Rule(name="user.choice")
        self._rule.enabled = True
        self._rule.action = self._default_action
        self._rule.duration = self._get_duration(self.durationCombo.currentIndex())

        what_idx = self.whatCombo.currentIndex()
        self._rule.operator.type, self._rule.operator.operand, self._rule.operator.data = self._get_combo_operator(self.whatCombo, what_idx)
        if self._rule.operator.data == "":
            print("Invalid rule, discarding: ", self._rule)
            self._rule = None
            self._done.set()
            return

        rule_temp_name = self._get_rule_name(self._rule)
        self._rule.name = rule_temp_name

        # TODO: move to a method
        data=[]
        if self._ischeckAdvanceded and self.checkDstIP.isChecked() and self.whatCombo.itemData(what_idx) != self.FIELD_DST_IP:
            _type, _operand, _data = self._get_combo_operator(self.whatIPCombo, self.whatIPCombo.currentIndex())
            data.append({"type": _type, "operand": _operand, "data": _data})
            rule_temp_name = slugify("%s %s" % (rule_temp_name, _data))

        if self._ischeckAdvanceded and self.checkDstPort.isChecked() and self.whatCombo.itemData(what_idx) != self.FIELD_DST_PORT:
            data.append({"type": "simple", "operand": "dest.port", "data": str(self._con.dst_port)})
            rule_temp_name = slugify("%s %s" % (rule_temp_name, str(self._con.dst_port)))

        if self._ischeckAdvanceded and self.checkUserID.isChecked() and self.whatCombo.itemData(what_idx) != self.FIELD_USER_ID:
            data.append({"type": "simple", "operand": "user.id", "data": str(self._con.user_id)})
            rule_temp_name = slugify("%s %s" % (rule_temp_name, str(self._con.user_id)))

        if self._ischeckAdvanceded:
            data.append({"type": self._rule.operator.type, "operand": self._rule.operator.operand, "data": self._rule.operator.data})
            self._rule.operator.data = json.dumps(data)
            self._rule.operator.type = "list"
            self._rule.operator.operand = ""

        self._rule.name = rule_temp_name

        self.hide()
        if self._ischeckAdvanceded:
            self.checkAdvanced.toggle()
        self._ischeckAdvanceded = False

        # signal that the user took a decision and
        # a new rule is available
        self._done.set()
