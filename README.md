# FlooCast

A Python application that allows for the control and configuration of FlooGoo USB Bluetooth dongles.

It configures a FlooGoo FMA120 Bluetooth dongle to pair and connect with a Bluetooth headset/speaker for streaming audio or making VoIP calls. It can also configure the dongle to work as an AuraCast sender.

The dongle functions as a standard USB audio speaker and microphone, requiring no drivers on Windows, macOS, or Linux.

**This fork adds macOS/Linux firmware upgrade support.** The official app relies on a Windows-only DLL for firmware updates. The included `hid_dfu.py` tool reimplements the Qualcomm VM Upgrade Protocol in pure Python, so you can update your dongle's firmware without needing a Windows machine.

## Installation

### Windows
On Windows, the compiled App can be downloaded directly from Microsoft Store.

### Linux/Mac

Requires python 3.7+

#### Configure Python Environment
Create a virtual environment and install the required packages.
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

#### Running
After configuring the environment, run `main.py` within it.
```bash
venv/bin/python main.py
```

If your dongle is not found, or if you see a permission denied error, check the [Platform specific notes/issues](#Platform-specific-notes/issues) section.
You may also choose to run the command as root to ensure it has access to the serial device.
```bash
sudo venv/bin/python main.py
```

## Usage

Once configured, the dongle can automatically reconnect to the most recently used device. Please check the support link for more advanced uses. 
 
## Platform specific notes/issues

On Linux, if you run the app as a non-root user, you might get "Permission denied: '/dev/ttyACM0'" error. 
Please verify the ttyACM0 device is the "dialout" user group and add your $USER to the group.
You may take the following link as a reference,
https://askubuntu.com/questions/133235/how-do-i-allow-non-root-access-to-ttyusb0

## Firmware Upgrade on macOS (without Windows)

The official FlooCast app only supports firmware upgrades on Windows. The included `hid_dfu.py` tool implements the Qualcomm VM Upgrade Protocol natively in Python, allowing firmware updates directly from macOS (or Linux).

### Supported Devices

| Device | Chip | USB VID:PID |
|--------|------|-------------|
| Flairmesh FlooGoo FMA120 | Qualcomm QCC3086 | 0A12:4007 |

Other Qualcomm QCC30xx/QCC51xx-based USB dongles using the same HID DFU protocol may also work.

### Quick Start

```bash
# 1. Set up the environment (one-time)
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Plug in the FMA120 dongle

# 3. Check current firmware version
python hid_dfu.py --version

# 4. Download the latest firmware
#    Go to: https://www.flairmesh.com/Dongle/FMA120.html
#    Or directly: https://www.flairmesh.com/support/FMA120_1.1.6.zip
#    Unzip and locate the .bin file matching your variant (e.g. FMA120_1_1_6G.bin)

# 5. Inspect the firmware file
python hid_dfu.py --info path/to/FMA120_1_1_6G.bin

# 6. Upgrade!
python hid_dfu.py path/to/FMA120_1_1_6G.bin
```

### What Happens During the Upgrade

1. **Connect** -- establishes HID communication with the dongle
2. **Sync** -- checks if a previous upgrade was interrupted and can be resumed
3. **Data transfer** -- sends firmware in device-requested chunks (~2 MB, takes 1-2 minutes)
4. **Validation** -- device verifies the received image
5. **Reboot** -- dongle reboots with new firmware (USB disconnects briefly)
6. **Commit** -- reconnects and confirms the upgrade

The dongle has dual-image support with self-recovery. If something goes wrong, it falls back to the previous firmware.

### Verbose Output

Add `-v` for detailed protocol logging (useful for debugging):

```bash
python hid_dfu.py -v path/to/FMA120_1_1_6G.bin
```

### Troubleshooting

**"Device not found"**
- Make sure the dongle is plugged in
- On macOS, no special drivers are needed -- the dongle shows up as a USB HID device
- Try unplugging and re-plugging the dongle
- Check with: `python -c "import hid; print([d for d in hid.enumerate(0x0A12, 0x4007)])"`

**"Read timeout"**
- Unplug and re-plug the dongle, then retry
- The dongle may not respond after a dirty disconnect -- a fresh plug-in resets it

**"Protocol version mismatch"**
- Make sure you're using the correct firmware variant for your dongle (check the suffix, e.g. "G")

**Upgrade interrupted / stuck**
- Just run the command again -- the tool detects the resume point and continues from where it left off

**Permission errors on Linux**
- Run with `sudo`, or add a udev rule for the device:
  ```
  echo 'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0a12", ATTRS{idProduct}=="4007", MODE="0666"' | sudo tee /etc/udev/rules.d/99-fma120.rules
  sudo udevadm control --reload-rules
  ```

### How It Works

The tool communicates with the dongle over USB HID (no serial port needed):

- **Report ID 3** (Feature Report) -- Connect/Disconnect handshake
- **Report ID 5** (Interrupt OUT) -- Upgrade protocol commands and firmware data
- **Report ID 6** (Interrupt IN) -- Device responses

The protocol was reverse-engineered from the [fwupd qc-s5gen2 plugin](https://github.com/fwupd/fwupd/tree/main/plugins/qc-s5gen2) by Denis Pynkin and Richard Hughes.

## Acknowledgements

- [fwupd](https://github.com/fwupd/fwupd) -- The qc-s5gen2 plugin provided the protocol reference for the macOS DFU implementation
