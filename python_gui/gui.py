import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import tkinter as tk
from tkinter import ttk, messagebox

# ---------------------- Helper: parse JSON objects from stream ----------------------
class JsonObjectReader:
    """Membaca objek JSON bahkan jika dicetak pretty (multi-baris).
    Menggunakan hitung-brace sederhana (asumsi tidak ada '}' dalam string).
    """
    def __init__(self, stream):
        self.stream = stream
        self.buf = []
        self.depth = 0
        self.in_obj = False

    def _feed(self, chunk):
        objs = []
        for ch in chunk:
            if ch == '{':
                self.depth += 1
                self.in_obj = True
                self.buf.append(ch)
            elif ch == '}':
                self.buf.append(ch)
                self.depth -= 1
                if self.in_obj and self.depth == 0:
                    objs.append(''.join(self.buf))
                    self.buf.clear()
                    self.in_obj = False
            else:
                if self.in_obj:
                    self.buf.append(ch)
        return objs

    def iter_objects(self, stop_event):
        # Baca per-chunk agar responsif (Rust stdout bisa buffered saat dipipe)
        while not stop_event.is_set():
            ch = self.stream.read(1)
            if not ch:
                # proses mungkin selesai; kecilkan jeda & cek lagi
                time.sleep(0.02)
                continue
            objs = self._feed(ch)
            for obj in objs:
                yield obj

# ---------------------- Worker process ----------------------
class NpkProcess:
    def __init__(self, bin_path, port, baud, unit, n_reg, p_reg, k_reg, reg_width, interval):
        self.bin_path = bin_path
        self.args = [
            bin_path,
            "--port", port,
            "--baud", str(baud),
            "--unit", str(unit),
            "--n-reg", str(n_reg),
            "--p-reg", str(p_reg),
            "--k-reg", str(k_reg),
            "--reg-width", str(reg_width),
            "--interval", str(interval),
        ]
        self.proc = None
        self.thread = None
        self.q = queue.Queue()
        self.stop_event = threading.Event()

    def start(self):
        try:
            self.proc = subprocess.Popen(
                self.args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,  # text mode + line-buffered (best effort)
                universal_newlines=True,
            )
        except FileNotFoundError:
            raise RuntimeError(f"Binary tidak ditemukan: {self.bin_path}")
        except Exception as e:
            raise RuntimeError(f"Gagal menjalankan binary: {e}")

        def reader():
            rdr = JsonObjectReader(self.proc.stdout)
            for obj_text in rdr.iter_objects(self.stop_event):
                try:
                    data = json.loads(obj_text)
                    # validasi minimal
                    if all(k in data for k in ("n", "p", "k")):
                        self.q.put((time.monotonic(), int(data["n"]), int(data["p"]), int(data["k"])) )
                except json.JSONDecodeError:
                    # Abaikan potongan yang bukan objek lengkap
                    pass
            # drain terakhir jika proses berakhir

        self.thread = threading.Thread(target=reader, daemon=True)
        self.thread.start()

    def poll_stderr(self):
        """Ambil beberapa baris error (non-blocking) untuk ditampilkan opsional."""
        msgs = []
        try:
            if self.proc and self.proc.stderr:
                while True:
                    line = self.proc.stderr.readline()
                    if not line:
                        break
                    line = line.strip()
                    if line:
                        msgs.append(line)
        except Exception:
            pass
        return msgs

    def stop(self):
        self.stop_event.set()
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass

# ---------------------- GUI ----------------------
class App(tk.Tk):
    def __init__(self, defaults):
        super().__init__()
        self.title("NPK Live — Python GUI")
        self.geometry("980x600")

        # State
        self.proc = None
        self.t0 = None
        self.max_points = 1800  # ~30 menit @1 Hz
        self.t_buf = deque(maxlen=self.max_points)
        self.n_buf = deque(maxlen=self.max_points)
        self.p_buf = deque(maxlen=self.max_points)
        self.k_buf = deque(maxlen=self.max_points)

        # Top panel (controls)
        self.ctrl = ttk.Frame(self)
        self.ctrl.pack(side=tk.TOP, fill=tk.X, padx=10, pady=8)

        self.bin_var = tk.StringVar(value=defaults['bin'])
        self.port_var = tk.StringVar(value=defaults['port'])
        self.baud_var = tk.IntVar(value=defaults['baud'])
        self.unit_var = tk.IntVar(value=defaults['unit'])
        self.nreg_var = tk.StringVar(value=defaults['n_reg'])
        self.preg_var = tk.StringVar(value=defaults['p_reg'])
        self.kreg_var = tk.StringVar(value=defaults['k_reg'])
        self.width_var = tk.IntVar(value=defaults['reg_width'])
        self.intv_var = tk.IntVar(value=defaults['interval'])

        def add_labeled(entry_var, label, width=14):
            f = ttk.Frame(self.ctrl)
            f.pack(side=tk.LEFT, padx=4)
            ttk.Label(f, text=label).pack(anchor=tk.W)
            if isinstance(entry_var, tk.IntVar):
                e = ttk.Entry(f, textvariable=entry_var, width=width)
            else:
                e = ttk.Entry(f, textvariable=entry_var, width=width)
            e.pack()
            return e

        add_labeled(self.bin_var, "Binary path", width=38)
        add_labeled(self.port_var, "Port")
        add_labeled(self.baud_var, "Baud")
        add_labeled(self.unit_var, "Unit")
        add_labeled(self.nreg_var, "N reg (hex)")
        add_labeled(self.preg_var, "P reg (hex)")
        add_labeled(self.kreg_var, "K reg (hex)")
        add_labeled(self.width_var, "reg_width (1/2)")
        add_labeled(self.intv_var, "interval (s)")

        btns = ttk.Frame(self.ctrl)
        btns.pack(side=tk.LEFT, padx=6)
        self.start_btn = ttk.Button(btns, text="Start", command=self.on_start)
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.on_stop, state=tk.DISABLED)
        self.clear_btn = ttk.Button(btns, text="Clear", command=self.on_clear)
        self.start_btn.grid(row=0, column=0, padx=2)
        self.stop_btn.grid(row=0, column=1, padx=2)
        self.clear_btn.grid(row=0, column=2, padx=2)

        # Value badges
        self.badges = ttk.Frame(self)
        self.badges.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0,8))
        self.lbl_n = ttk.Label(self.badges, text="N: – mg/kg", font=("Segoe UI", 12, "bold"))
        self.lbl_p = ttk.Label(self.badges, text="P: – mg/kg", font=("Segoe UI", 12, "bold"))
        self.lbl_k = ttk.Label(self.badges, text="K: – mg/kg", font=("Segoe UI", 12, "bold"))
        self.lbl_n.pack(side=tk.LEFT, padx=8)
        self.lbl_p.pack(side=tk.LEFT, padx=8)
        self.lbl_k.pack(side=tk.LEFT, padx=8)

        # Matplotlib figure
        self.fig = Figure(figsize=(8, 4), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_title("NPK vs Time (s)")
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("mg/kg")
        (self.line_n,) = self.ax.plot([], [], label="N")
        (self.line_p,) = self.ax.plot([], [], label="P")
        (self.line_k,) = self.ax.plot([], [], label="K")
        self.ax.legend()
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=1)

        # Schedule UI update
        self.after(100, self.on_tick)

    # ------------------ Handlers ------------------
    def on_start(self):
        if self.proc is not None:
            return
        bin_path = self.bin_var.get().strip()
        if not bin_path:
            messagebox.showerror("Error", "Isi 'Binary path' terlebih dahulu")
            return
        if not os.path.exists(bin_path):
            messagebox.showerror("Error", f"Binary tidak ditemukan: {bin_path}")
            return
        try:
            port = self.port_var.get().strip()
            baud = int(self.baud_var.get())
            unit = int(self.unit_var.get())
            n_reg = int(self.nreg_var.get(), 16) if self.nreg_var.get().startswith("0x") else int(self.nreg_var.get())
            p_reg = int(self.preg_var.get(), 16) if self.preg_var.get().startswith("0x") else int(self.preg_var.get())
            k_reg = int(self.kreg_var.get(), 16) if self.kreg_var.get().startswith("0x") else int(self.kreg_var.get())
            reg_width = int(self.width_var.get())
            interval = int(self.intv_var.get())
        except ValueError:
            messagebox.showerror("Error", "Nilai numerik tidak valid")
            return
        self.proc = NpkProcess(bin_path, port, baud, unit, n_reg, p_reg, k_reg, reg_width, interval)
        try:
            self.proc.start()
        except Exception as e:
            self.proc = None
            messagebox.showerror("Error", str(e))
            return
        self.t0 = time.monotonic()
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

    def on_stop(self):
        if self.proc:
            self.proc.stop()
            self.proc = None
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)

    def on_clear(self):
        self.t_buf.clear(); self.n_buf.clear(); self.p_buf.clear(); self.k_buf.clear()
        self._redraw()
        self.lbl_n.config(text="N: – mg/kg")
        self.lbl_p.config(text="P: – mg/kg")
        self.lbl_k.config(text="K: – mg/kg")

    def on_tick(self):
        # Ambil data baru dari queue
        if self.proc:
            try:
                while True:
                    t, n, p, k = self.proc.q.get_nowait()
                    t_rel = t - self.t0 if self.t0 else 0.0
                    self.t_buf.append(t_rel)
                    self.n_buf.append(n)
                    self.p_buf.append(p)
                    self.k_buf.append(k)
            except queue.Empty:
                pass

            # Update label nilai terakhir
            if self.n_buf:
                self.lbl_n.config(text=f"N: {self.n_buf[-1]} mg/kg")
                self.lbl_p.config(text=f"P: {self.p_buf[-1]} mg/kg")
                self.lbl_k.config(text=f"K: {self.k_buf[-1]} mg/kg")

        self._redraw()
        self.after(100, self.on_tick)

    def _redraw(self):
        self.line_n.set_data(self.t_buf, self.n_buf)
        self.line_p.set_data(self.t_buf, self.p_buf)
        self.line_k.set_data(self.t_buf, self.k_buf)
        self.ax.relim()
        self.ax.autoscale_view()
        self.canvas.draw_idle()


def guess_default_bin():
    # Coba path relatif ke proyek Rust
    if os.name == 'nt':
        candidate = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "target", "release", "npk-reader.exe"))
    else:
        candidate = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "target", "release", "npk-reader"))
    return candidate


def main():
    parser = argparse.ArgumentParser(description="NPK Python GUI")
    parser.add_argument("--bin", default=guess_default_bin(), help="Path ke binary npk-reader")
    parser.add_argument("--port", default=("COM6" if os.name == 'nt' else "/dev/ttyUSB0"))
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--unit", type=int, default=1)
    parser.add_argument("--n-reg", dest="n_reg", default="0x001E")
    parser.add_argument("--p-reg", dest="p_reg", default="0x001F")
    parser.add_argument("--k-reg", dest="k_reg", default="0x0020")
    parser.add_argument("--reg-width", dest="reg_width", type=int, default=1)
    parser.add_argument("--interval", type=int, default=1)
    args = parser.parse_args()

    defaults = {
        'bin': args.bin,
        'port': args.port,
        'baud': args.baud,
        'unit': args.unit,
        'n_reg': str(args.n_reg),
        'p_reg': str(args.p_reg),
        'k_reg': str(args.k_reg),
        'reg_width': args.reg_width,
        'interval': args.interval,
    }

    app = App(defaults)
    app.mainloop()


if __name__ == "__main__":
    main()

# Windows: python gui.py --port COM6 --baud 9600 --unit 1 --interval 1
# Linux: python gui.py --port /dev/ttyUSB0