"""
Desktop GUI for DXoIP RoIP Audio Simulator (gui.py)
Provides a graphical Push-to-Talk button (Hold to Talk via mouse or Spacebar),
real-time VU meters, and carrier squelch (COS) indicator.
Uses Python's standard tkinter library (zero extra dependencies).
"""

import sys
import time

try:
    import tkinter as tk
    from tkinter import ttk
    TKINTER_AVAILABLE = True
except ImportError:
    TKINTER_AVAILABLE = False


def launch_gui(sim):
    """Launches the Tkinter GUI for the RoIP Audio Simulator."""
    if not TKINTER_AVAILABLE:
        raise RuntimeError("Tkinter is not installed in this Python environment.")

    root = tk.Tk()
    root.title("DXoIP RoIP Audio Simulator")
    root.geometry("450x440")
    root.resizable(False, False)

    # Bring window to front across macOS and other desktop platforms
    root.lift()
    try:
        root.attributes("-topmost", True)
        root.after_idle(root.attributes, "-topmost", False)
        root.focus_force()
    except Exception:
        pass

    # Style
    style = ttk.Style()
    style.theme_use("clam")

    # Header frame
    header_frame = ttk.Frame(root, padding=10)
    header_frame.pack(fill=tk.X)

    title_lbl = ttk.Label(header_frame, text="DXoIP RoIP Audio Simulator", font=("Helvetica", 14, "bold"))
    title_lbl.pack(anchor="w")

    status_var = tk.StringVar(value="Status: Waiting for peer to connect...")
    status_lbl = ttk.Label(header_frame, textvariable=status_var, font=("Helvetica", 10, "bold"), foreground="#e65100")
    status_lbl.pack(anchor="w")

    peer_var = tk.StringVar(value="Peer: Connecting...")
    peer_lbl = ttk.Label(header_frame, textvariable=peer_var, font=("Helvetica", 9))
    peer_lbl.pack(anchor="w")

    backend_lbl = ttk.Label(header_frame, text=f"Audio Engine: {sim.audio_backend.__class__.__name__}", font=("Helvetica", 9, "italic"))
    backend_lbl.pack(anchor="w")

    ttk.Separator(root, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=10, pady=5)

    # Status / Indicators frame
    status_frame = ttk.Frame(root, padding=10)
    status_frame.pack(fill=tk.X)

    cos_var = tk.StringVar(value="SQUELCH CLOSED")
    cos_lbl = tk.Label(status_frame, textvariable=cos_var, bg="#444444", fg="white", font=("Helvetica", 10, "bold"), width=25, height=2)
    cos_lbl.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

    remote_ptt_var = tk.StringVar(value="REMOTE PTT: IDLE")
    remote_ptt_lbl = tk.Label(status_frame, textvariable=remote_ptt_var, bg="#444444", fg="white", font=("Helvetica", 10, "bold"), width=25, height=2)
    remote_ptt_lbl.pack(side=tk.RIGHT, padx=5, fill=tk.X, expand=True)

    # Push-to-Talk Button (Large, prominent)
    ptt_frame = ttk.Frame(root, padding=15)
    ptt_frame.pack(fill=tk.BOTH, expand=True)

    ptt_btn = tk.Button(
        ptt_frame,
        text="PUSH TO TALK\n(Hold Spacebar or Click & Hold)",
        font=("Helvetica", 13, "bold"),
        bg="#2e7d32",
        fg="white",
        activebackground="#c62828",
        activeforeground="white",
        relief=tk.RAISED,
        bd=4,
    )
    ptt_btn.pack(fill=tk.BOTH, expand=True)

    # Event handlers for true Push-to-Talk
    def on_ptt_press(event=None):
        sim.key_ptt()
        ptt_btn.config(bg="#c62828", text="TRANSMITTING...\n(PTT ACTIVE)")

    def on_ptt_release(event=None):
        sim.unkey_ptt()
        ptt_btn.config(bg="#2e7d32", text="PUSH TO TALK\n(Hold Spacebar or Click & Hold)")

    # Mouse press and release
    ptt_btn.bind("<ButtonPress-1>", on_ptt_press)
    ptt_btn.bind("<ButtonRelease-1>", on_ptt_release)

    # Keyboard Spacebar press and release on root window
    root.bind("<KeyPress-space>", on_ptt_press)
    root.bind("<KeyRelease-space>", on_ptt_release)

    # Audio VU Meters frame
    vu_frame = ttk.LabelFrame(root, text="Audio Levels", padding=10)
    vu_frame.pack(fill=tk.X, padx=10, pady=5)

    tx_vu_lbl = ttk.Label(vu_frame, text="Tx Audio (Mic):")
    tx_vu_lbl.grid(row=0, column=0, sticky="w", pady=2)
    tx_vu_bar = ttk.Progressbar(vu_frame, orient=tk.HORIZONTAL, length=250, mode="determinate")
    tx_vu_bar.grid(row=0, column=1, padx=5, pady=2)

    rx_vu_lbl = ttk.Label(vu_frame, text="Rx Audio (Spk):")
    rx_vu_lbl.grid(row=1, column=0, sticky="w", pady=2)
    rx_vu_bar = ttk.Progressbar(vu_frame, orient=tk.HORIZONTAL, length=250, mode="determinate")
    rx_vu_bar.grid(row=1, column=1, padx=5, pady=2)

    # Control buttons (Tone burst, Heartbeat, Exit)
    btn_frame = ttk.Frame(root, padding=10)
    btn_frame.pack(fill=tk.X)

    def on_tone_burst():
        sim.trigger_tone_burst(duration_s=1.0)

    tone_btn = ttk.Button(btn_frame, text="1s Tone Burst (T)", command=on_tone_burst)
    tone_btn.pack(side=tk.LEFT, padx=5)

    def on_heartbeat():
        sim.roip.send_heartbeat(ptt=sim.ptt_active, cos=sim.jitter_buffer.is_receiving)

    hb_btn = ttk.Button(btn_frame, text="Send Heartbeat (H)", command=on_heartbeat)
    hb_btn.pack(side=tk.LEFT, padx=5)

    def on_close():
        sim.stop()
        root.destroy()

    quit_btn = ttk.Button(btn_frame, text="Quit", command=on_close)
    quit_btn.pack(side=tk.RIGHT, padx=5)

    root.protocol("WM_DELETE_WINDOW", on_close)

    # Periodic GUI update
    def update_gui():
        if not sim.running:
            root.destroy()
        # Update connection status & peer endpoint
        if sim.udp_manager.connected and sim.udp_manager.peer_addr:
            status_var.set("Status: CONNECTED (P2P Link Active)")
            status_lbl.config(foreground="#2e7d32")
            peer_var.set(f"Peer: {sim.udp_manager.peer_addr[0]}:{sim.udp_manager.peer_addr[1]}")
        else:
            status_var.set("Status: Waiting for peer to connect...")
            status_lbl.config(foreground="#e65100")
            peer_var.set("Peer: Connecting...")

        # Update Carrier squelch
        if sim.jitter_buffer.is_receiving:
            cos_var.set("CARRIER ACTIVE (COS)")
            cos_lbl.config(bg="#1565c0")
        else:
            cos_var.set("SQUELCH CLOSED")
            cos_lbl.config(bg="#444444")

        # Update remote PTT
        if sim.last_rx_remote_ptt:
            remote_ptt_var.set("REMOTE PTT: KEYED")
            remote_ptt_lbl.config(bg="#c62828")
        else:
            remote_ptt_var.set("REMOTE PTT: IDLE")
            remote_ptt_lbl.config(bg="#444444")

        # Update VU meters: map [-60 dB, 0 dB] -> [0, 100]
        tx_pct = max(0, min(100, int((sim.last_tx_db + 60.0) / 60.0 * 100)))
        rx_pct = max(0, min(100, int((sim.last_rx_db + 60.0) / 60.0 * 100)))
        tx_vu_bar["value"] = tx_pct
        rx_vu_bar["value"] = rx_pct

        # If PTT was triggered via burst, update button appearance
        if sim.is_transmitting and ptt_btn.cget("bg") != "#c62828":
            ptt_btn.config(bg="#c62828", text="TRANSMITTING...\n(BURST ACTIVE)")
        elif not sim.is_transmitting and ptt_btn.cget("bg") == "#c62828":
            ptt_btn.config(bg="#2e7d32", text="PUSH TO TALK\n(Hold Spacebar or Click & Hold)")

        root.after(50, update_gui)

    root.after(50, update_gui)
    root.mainloop()
