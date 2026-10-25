"""Tk controls for monitoring; playback and recording state lives in LiveMonitor."""

import copy
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from monitoring import DEFAULT_ANCHOR, QUALITY_NAMES, select_stream


class MonitorPanel(tk.LabelFrame):
    def __init__(self, parent, monitor, save, streams):
        super().__init__(parent, text='持续监控与录制', bg='#2d2d2d', fg='#cccccc',
                         font=('微软雅黑', 10, 'bold'))
        self.monitor = monitor
        self.save = save
        self.streams = streams
        self._rows = {}
        actions = tk.Frame(self, bg='#2d2d2d')
        actions.pack(fill=tk.X, padx=8, pady=6)
        self.toggle = self.button(actions, '启动监控', self.toggle_monitor)
        self.button(actions, '监控设置', self.show_settings)
        self.button(actions, '开始录制', self.start_recording)
        self.button(actions, '停止录制', self.stop_recording)
        self.button(actions, '打开录像目录', self.open_directory)
        self.anchor_enabled = tk.BooleanVar(value=False)
        self.anchor_toggle = tk.Checkbutton(
            self, text='将选中主播加入持续监控', variable=self.anchor_enabled,
            command=self.set_selected_monitoring, state=tk.DISABLED,
            bg='#2d2d2d', fg='#eeeeee', selectcolor='#3c3c3c',
            activebackground='#2d2d2d', activeforeground='#ffffff')
        self.anchor_toggle.pack(anchor='w', padx=8)
        self.summary = tk.Label(self, text='监控未启动 · 选择下方主播可单独开始或停止录制',
                                bg='#2d2d2d', fg='#cccccc', anchor='w')
        self.summary.pack(fill=tk.X, padx=8)
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('Monitor.Treeview', background='#252526', fieldbackground='#252526',
                        foreground='#eeeeee', rowheight=27, borderwidth=0)
        style.map('Monitor.Treeview', background=[('selected', '#005a99')],
                  foreground=[('selected', '#ffffff')])
        style.configure('Monitor.Treeview.Heading', background='#3c3c3c', foreground='#eeeeee')
        table = tk.Frame(self, bg='#2d2d2d')
        table.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)
        self.tree = ttk.Treeview(table, columns=('name', 'enabled', 'mode', 'live', 'messages', 'record', 'time'),
                                 show='headings', height=4, selectmode='browse', style='Monitor.Treeview')
        for key, title, width in [('name', '主播', 120), ('enabled', '监控名单', 75), ('mode', '开播操作', 100),
                                  ('live', '直播状态', 145), ('record', '录制状态', 160),
                                  ('messages', '消息采集', 110),
                                  ('time', '检测时间', 70)]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=60)
        scrollbar = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.tree.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind('<Double-1>', lambda event: self.show_settings())
        self.tree.bind('<<TreeviewSelect>>', self.sync_selected_monitoring)

    def sync_selected_monitoring(self, event=None):
        selection = self.tree.selection()
        rid = selection[0] if selection else None
        self.anchor_toggle.config(state=tk.NORMAL if rid in self.monitor.anchors else tk.DISABLED)
        self.anchor_enabled.set(bool(rid in self.monitor.anchors and self.monitor.options(rid)['enabled']))

    def set_selected_monitoring(self):
        selection = self.tree.selection()
        if not selection or selection[0] not in self.monitor.anchors:
            return
        rid = selection[0]
        settings = copy.deepcopy(self.monitor.settings)
        settings.setdefault('anchors', {}).setdefault(rid, {})['enabled'] = self.anchor_enabled.get()
        try:
            self.save(settings)
        except OSError as exc:
            messagebox.showerror('保存失败', str(exc), parent=self)
        self.refresh()

    @staticmethod
    def button(parent, text, command):
        button = tk.Button(parent, text=text, command=command, bg='#3c3c3c', fg='#ffffff',
                           activebackground='#005a99', activeforeground='#ffffff',
                           relief=tk.FLAT, padx=8, pady=3, cursor='hand2')
        button.pack(side=tk.LEFT, padx=(0, 5))
        return button

    def selected(self):
        selection = self.tree.selection()
        if selection:
            return selection[0]
        messagebox.showinfo('选择主播', '请在监控列表中选择一个主播', parent=self)
        return None

    def toggle_monitor(self):
        if self.monitor.enabled:
            self.monitor.stop()
        elif not any(self.monitor.options(rid)['enabled'] for rid in self.monitor.anchors):
            messagebox.showinfo('启动监控', '请先在下方列表选择主播，勾选“将选中主播加入持续监控”', parent=self)
        else:
            self.monitor.start()
        self.refresh()

    def start_recording(self):
        rid = self.selected()
        if not rid:
            return
        if rid in self.monitor.recordings:
            messagebox.showinfo('录制', '该主播已在录制或正在停止，请稍后再试', parent=self)
            return
        url = select_stream(self.streams(rid), self.monitor.options(rid)['quality'])
        if not url:
            messagebox.showinfo('录制', '没有直播流地址，请先启动监控或一键检测', parent=self)
            return
        try:
            self.monitor.start_recording(rid, url)
        except OSError as exc:
            messagebox.showerror('录制失败', str(exc), parent=self)
        self.refresh()

    def stop_recording(self):
        rid = self.selected()
        if rid:
            self.monitor.stop_recording(rid)
            self.refresh()

    def open_directory(self):
        path = os.path.abspath(os.path.expanduser(self.monitor.settings['directory']))
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)
        except OSError as exc:
            messagebox.showerror('打开目录失败', str(exc), parent=self)

    def refresh(self):
        monitor = self.monitor
        self.toggle.config(text='停止监控' if monitor.enabled else '启动监控',
                           bg='#007acc' if monitor.enabled else '#3c3c3c')
        count = sum(monitor.options(rid)['enabled'] for rid in monitor.anchors)
        self.summary.config(text=(f'监控中 · {count} 位主播 · 每 {monitor.settings["interval"]} 秒检测'
                                  if monitor.enabled else f'监控未启动 · 已选择 {count} 位主播')
                            + f' · {len(monitor.recordings)} 个录制任务')
        rows = {}
        for rid, name in monitor.anchors.items():
            options = monitor.options(rid)
            mode = ('播放 ' if options['auto_play'] else '') + ('录制' if options['auto_record'] else '')
            if options['auto_collect']:
                mode += ' 采集消息'
            mode = (mode.strip() or '仅检测') if options['enabled'] else '不监控'
            state = monitor.states.get(rid)
            record = state.recording_status if state else ''
            if state and state.suppressed and rid not in monitor.recordings:
                record = '本场自动录制已暂停'
            rows[rid] = (name, '已加入' if options['enabled'] else '未加入', mode, state.status if state else '尚未检测',
                         monitor.collector.status(rid) if monitor.collector else '未采集',
                         record or '未录制', state.last_check if state else '')
        for rid in self._rows.keys() - rows.keys():
            self.tree.delete(rid)
        for rid, values in rows.items():
            if rid not in self._rows:
                self.tree.insert('', tk.END, iid=rid, values=values)
            elif self._rows[rid] != values:
                self.tree.item(rid, values=values)
        self._rows = rows
        self.sync_selected_monitoring()

    def show_settings(self):
        win = tk.Toplevel(self)
        win.title('监控设置')
        win.configure(bg='#2d2d2d')
        win.transient(self.winfo_toplevel())
        win.grab_set()
        win.columnconfigure(1, weight=1)
        settings = copy.deepcopy(self.monitor.settings)
        fields = {}

        def label(text, row):
            tk.Label(win, text=text, bg='#2d2d2d', fg='#eeeeee').grid(
                row=row, column=0, sticky='w', padx=12, pady=6)

        for row, (key, title) in enumerate([('interval', '检测间隔（秒）'), ('directory', '录像保存目录'),
                                            ('ffmpeg', 'FFmpeg 程序路径'), ('potplayer', 'PotPlayer 程序路径')]):
            label(title, row)
            var = tk.StringVar(value=str(settings[key]))
            fields[key] = var
            tk.Entry(win, textvariable=var, width=48, bg='#3c3c3c', fg='#ffffff',
                     insertbackground='#ffffff').grid(row=row, column=1, sticky='ew', padx=8, pady=6)
            if key != 'interval':
                def browse(k=key, v=var):
                    value = (filedialog.askdirectory(parent=win) if k == 'directory'
                             else filedialog.askopenfilename(parent=win, filetypes=[('程序', '*.exe')]))
                    if value:
                        v.set(value)
                tk.Button(win, text='浏览', command=browse).grid(row=row, column=2, padx=8)

        tk.Label(win, text='程序路径留空时自动查找；检测间隔为 15–3600 秒。', bg='#2d2d2d',
                 fg='#cccccc').grid(row=4, column=0, columnspan=3, sticky='w', padx=12, pady=6)
        label('主播', 5)
        names = [f'{name}（{rid}）' for rid, name in self.monitor.anchors.items()]
        rids = list(self.monitor.anchors)
        anchor = ttk.Combobox(win, values=names, state='readonly', width=40)
        anchor.grid(row=5, column=1, sticky='ew', padx=8)
        variables = {key: tk.BooleanVar(value=DEFAULT_ANCHOR[key])
                     for key in ('enabled', 'auto_play', 'auto_record', 'auto_collect')}
        for row, (key, title) in enumerate([('enabled', '持续监控此主播'),
                                            ('auto_play', '开播自动打开 PotPlayer'),
                                            ('auto_record', '开播自动使用 FFmpeg 录制'),
                                            ('auto_collect', '开播自动采集弹幕、礼物等消息')], 6):
            tk.Checkbutton(win, text=title, variable=variables[key], bg='#2d2d2d', fg='#eeeeee',
                           selectcolor='#3c3c3c', activebackground='#2d2d2d').grid(
                               row=row, column=1, sticky='w', padx=8, pady=3)
        label('清晰度', 10)
        qualities = {'best': '最高可用', **QUALITY_NAMES}
        quality = ttk.Combobox(win, values=list(qualities.values()), state='readonly')
        quality.grid(row=10, column=1, sticky='w', padx=8)
        current = [None]

        def persist_anchor():
            if current[0] is not None:
                settings.setdefault('anchors', {})[current[0]] = {
                    **{key: var.get() for key, var in variables.items()},
                    'quality': list(qualities)[max(0, quality.current())]}

        def select_anchor(event=None):
            persist_anchor()
            if anchor.current() < 0:
                return
            rid = rids[anchor.current()]
            current[0] = rid
            options = {**DEFAULT_ANCHOR, **settings.get('anchors', {}).get(rid, {})}
            for key, var in variables.items():
                var.set(options[key])
            quality.set(qualities.get(options['quality'], qualities['best']))

        anchor.bind('<<ComboboxSelected>>', select_anchor)
        if rids:
            selected = self.tree.selection()
            anchor.current(rids.index(selected[0]) if selected and selected[0] in rids else 0)
            select_anchor()
        tk.Label(win, text='录制与观看独立运行。指定清晰度不可用时使用最高可用清晰度。\n'
                 '仅勾选的主播参与持续监控；取消监控后，已有播放和录制继续。\n'
                 '停止监控不会停止录像；手动停止录制后，本场直播不再自动重启录像。\n'
                 '消息采集需启动监控、将主播加入监控并开启自动采集；点击主播仅查看状态。\n'
                 '停止监控、取消主播监控或关闭自动采集会停止对应消息连接及重连。\n'
                 '消息区显示所有采集中的主播，标注来源；下方勾选框只过滤显示，不影响存档。\n'
                 'PotPlayer 保存流：播放后按 Ctrl+A 设置，Ctrl+Shift+A 开始或停止。\n'
                 'PotPlayer 自动录制尚未启用；自动录制使用 FFmpeg。',
                 bg='#2d2d2d', fg='#cccccc', justify=tk.LEFT).grid(
                     row=11, column=0, columnspan=3, sticky='w', padx=12, pady=12)

        def save():
            try:
                interval = int(fields['interval'].get())
                if not 15 <= interval <= 3600:
                    raise ValueError()
            except ValueError:
                messagebox.showerror('设置错误', '检测间隔必须是 15–3600 的整数', parent=win)
                return
            directory = fields['directory'].get().strip()
            if not directory:
                messagebox.showerror('设置错误', '请选择录像保存目录', parent=win)
                return
            persist_anchor()
            settings.update({key: var.get().strip() for key, var in fields.items()})
            settings['interval'] = interval
            try:
                self.save(settings)
            except OSError as exc:
                messagebox.showerror('保存失败', str(exc), parent=win)
                return
            self.refresh()
            win.destroy()

        buttons = tk.Frame(win, bg='#2d2d2d')
        buttons.grid(row=12, column=0, columnspan=3, pady=12)
        self.button(buttons, '保存设置', save)
        self.button(buttons, '取消', win.destroy)
