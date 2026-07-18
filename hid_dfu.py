#!/usr/bin/env python3
#
# SPDX-License-Identifier: MIT
#
# HID DFU Tool for Qualcomm QCC3086 (FMA120) - macOS/Linux native implementation.
# Copyright (c) 2026 Robin Bially
#
# Protocol reverse-engineered from the fwupd qc-s5gen2 plugin by
# Denis Pynkin and Richard Hughes (LGPL-2.1-or-later):
# https://github.com/fwupd/fwupd/tree/main/plugins/qc-s5gen2
#
# Usage:
#     python hid_dfu.py firmware.bin          # Upgrade firmware
#     python hid_dfu.py --version             # Read device firmware version
#     python hid_dfu.py --info firmware.bin   # Show firmware file info
#

import sys
import struct
import time
import zlib
import argparse

import hid

# Device identifiers
VID = 0x0A12
PID = 0x4007
USAGE_PAGE = 0xFF00

# HID Report IDs
REPORT_CMD = 0x03          # Feature report: Connect/Disconnect (63 bytes total)
REPORT_DATA_OUT = 0x05     # Interrupt OUT: VMU messages to device (255 bytes total)
REPORT_DATA_IN = 0x06      # Interrupt IN: VMU responses from device (13 bytes total)

# Report sizes (excluding report ID byte which hidapi handles)
CMD_PAYLOAD_SIZE = 61
DATA_OUT_PAYLOAD_SIZE = 253
DATA_IN_PAYLOAD_SIZE = 11

# HID-level request types
REQ_CONNECT = 0x02
REQ_DISCONNECT = 0x07

# Connection status
STATUS_SUCCESS = 0x00
STATUS_ALREADY_CONNECTED = 0x02

# VM Upgrade Protocol OpCodes
OP_START_REQ = 0x01
OP_START_CFM = 0x02
OP_DATA_BYTES_REQ = 0x03
OP_DATA = 0x04
OP_ABORT_REQ = 0x07
OP_ABORT_CFM = 0x08
OP_TRANSFER_COMPLETE_IND = 0x0B
OP_TRANSFER_COMPLETE_RES = 0x0C
OP_PROCEED_TO_COMMIT = 0x0E
OP_COMMIT_REQ = 0x0F
OP_COMMIT_CFM = 0x10
OP_ERROR_IND = 0x11
OP_COMPLETE_IND = 0x12
OP_SYNC_REQ = 0x13
OP_SYNC_CFM = 0x14
OP_START_DATA_REQ = 0x15
OP_IS_VALIDATION_DONE_REQ = 0x16
OP_IS_VALIDATION_DONE_CFM = 0x17
OP_HOST_VERSION_REQ = 0x19
OP_HOST_VERSION_CFM = 0x1A
OP_ERROR_RES = 0x1F

# Resume points
RESUME_START = 0
RESUME_PRE_VALIDATE = 1
RESUME_PRE_REBOOT = 2
RESUME_POST_REBOOT = 3
RESUME_COMMIT = 4
RESUME_POST_COMMIT = 5

RESUME_NAMES = {
    0: "Start", 1: "PreValidate", 2: "PreReboot",
    3: "PostReboot", 4: "Commit", 5: "PostCommit",
}

OPCODE_NAMES = {
    0x01: "START_REQ", 0x02: "START_CFM", 0x03: "DATA_BYTES_REQ",
    0x04: "DATA", 0x07: "ABORT_REQ", 0x08: "ABORT_CFM",
    0x0B: "TRANSFER_COMPLETE_IND", 0x0C: "TRANSFER_COMPLETE_RES",
    0x0E: "PROCEED_TO_COMMIT", 0x0F: "COMMIT_REQ", 0x10: "COMMIT_CFM",
    0x11: "ERROR_IND", 0x12: "COMPLETE_IND", 0x13: "SYNC_REQ",
    0x14: "SYNC_CFM", 0x15: "START_DATA_REQ",
    0x16: "IS_VALIDATION_DONE_REQ", 0x17: "IS_VALIDATION_DONE_CFM",
    0x19: "HOST_VERSION_REQ", 0x1A: "HOST_VERSION_CFM", 0x1F: "ERROR_RES",
}

# Timing
SEND_DELAY_MS = 2
DATA_REQ_SLEEP_MS = 1000
VALIDATION_RETRIES = 600
HID_TIMEOUT_MS = 5000
ABORT_RETRIES = 67
ABORT_DELAY_MS = 300


class DfuError(Exception):
    pass


class HidDfuDevice:
    """Qualcomm QCC3086 HID DFU device."""

    def __init__(self, verbose=False):
        self.dev = None
        self.verbose = verbose

    def log(self, msg):
        if self.verbose:
            print(f"  [{time.strftime('%H:%M:%S')}] {msg}")

    def open(self):
        """Open the HID device on interface 1 (Usage Page 0xFF00)."""
        for info in hid.enumerate(VID, PID):
            if info['usage_page'] == USAGE_PAGE:
                self.log(f"Found device: {info['path']}")
                self.dev = hid.device()
                self.dev.open_path(info['path'])
                self.dev.set_nonblocking(0)
                return
        raise DfuError(f"Device VID={VID:#06x} PID={PID:#06x} not found")

    def close(self):
        if self.dev:
            self.dev.close()
            self.dev = None

    def _send_feature(self, data):
        """Send a Feature SET report (Report ID 3)."""
        # hidapi: first byte is report ID, then payload_len, then payload
        buf = bytearray(1 + 1 + CMD_PAYLOAD_SIZE)  # 63 bytes total
        buf[0] = REPORT_CMD
        buf[1] = len(data)
        buf[2:2 + len(data)] = data
        self.log(f"TX Feature[{REPORT_CMD}] len={len(data)}: {data.hex()}")
        self.dev.send_feature_report(buf)

    def _send_data(self, data):
        """Send an Interrupt OUT report (Report ID 5)."""
        # hidapi hid_write: first byte is report ID
        buf = bytearray(1 + 1 + DATA_OUT_PAYLOAD_SIZE)  # 255 bytes total
        buf[0] = REPORT_DATA_OUT
        buf[1] = len(data)
        buf[2:2 + len(data)] = data
        self.log(f"TX Data[{REPORT_DATA_OUT}] len={len(data)}: {data[:min(len(data),32)].hex()}{'...' if len(data) > 32 else ''}")
        self.dev.write(buf)

    def _recv(self, timeout_ms=HID_TIMEOUT_MS):
        """Read an Interrupt IN report (Report ID 6), return payload bytes."""
        # Full report: [report_id=0x06][payload_len][payload[11]] = 13 bytes
        # macOS hidapi may include report ID, so read the full 13 bytes
        data = self.dev.read(1 + 1 + DATA_IN_PAYLOAD_SIZE, timeout_ms)  # 13 bytes
        if not data:
            raise DfuError("Read timeout - no response from device")
        # Handle both cases: with and without report ID
        if len(data) >= 2:
            if data[0] == REPORT_DATA_IN:
                # Report ID present (macOS)
                payload_len = data[1]
                payload = bytes(data[2:2 + payload_len])
            else:
                # Report ID stripped by hidapi (Linux)
                payload_len = data[0]
                payload = bytes(data[1:1 + payload_len])
        else:
            raise DfuError(f"Short read: {len(data)} bytes")
        self.log(f"RX len={payload_len}: {payload.hex()}")
        return payload

    # --- HID-level connect/disconnect ---

    def connect(self):
        """Send HID connect request."""
        self.log("Sending CONNECT request...")
        req = struct.pack('>BH', REQ_CONNECT, 0)  # [0x02, 0x00, 0x00]
        self._send_feature(req)

        # Read status response
        resp = self._recv()
        if len(resp) < 1:
            raise DfuError("Empty connect response")
        status = resp[0]
        if status == STATUS_SUCCESS:
            self.log("Connected successfully")
        elif status == STATUS_ALREADY_CONNECTED:
            self.log("Already connected (warning, continuing)")
        else:
            raise DfuError(f"Connect failed with status: {status:#04x}")

    def disconnect(self):
        """Send HID disconnect request."""
        self.log("Sending DISCONNECT request...")
        req = struct.pack('>BH', REQ_DISCONNECT, 0)  # [0x07, 0x00, 0x00]
        self._send_feature(req)

    # --- VMU protocol message helpers ---

    def _vmu_send(self, opcode, payload=b''):
        """Build and send a VMU protocol message via Report ID 5."""
        msg = struct.pack('>BH', opcode, len(payload)) + payload
        self._send_data(msg)

    def _vmu_recv(self, timeout_ms=HID_TIMEOUT_MS):
        """Receive and parse a VMU protocol message. Returns (opcode, payload)."""
        data = self._recv(timeout_ms)
        if len(data) < 3:
            raise DfuError(f"Short VMU response: {data.hex()}")
        opcode = data[0]
        data_len = struct.unpack('>H', data[1:3])[0]
        payload = data[3:3 + data_len]
        name = OPCODE_NAMES.get(opcode, f"0x{opcode:02x}")
        self.log(f"VMU {name}: payload={payload.hex() if payload else '(empty)'}")
        return opcode, payload

    def _vmu_recv_check_error(self, timeout_ms=HID_TIMEOUT_MS):
        """Receive VMU message, auto-handle ERROR_IND."""
        opcode, payload = self._vmu_recv(timeout_ms)
        if opcode == OP_ERROR_IND:
            error_code = struct.unpack('>H', payload[:2])[0] if len(payload) >= 2 else 0xFFFF
            # Acknowledge the error
            self._vmu_send(OP_ERROR_RES, struct.pack('>H', error_code))
            raise DfuError(f"Device reported error: {error_code:#06x}")
        return opcode, payload

    # --- VM Upgrade Protocol commands ---

    def cmd_version(self):
        """Get device firmware version."""
        self._vmu_send(OP_HOST_VERSION_REQ)
        opcode, payload = self._vmu_recv_check_error()
        if opcode != OP_HOST_VERSION_CFM or len(payload) < 6:
            raise DfuError(f"Unexpected version response: opcode={opcode:#04x}")
        major, minor, config = struct.unpack('>HHH', payload[:6])
        return f"{major}.{minor}.{config}"

    def cmd_sync(self, file_id):
        """Send SYNC_REQ, return (resume_point, file_id, protocol_version)."""
        self._vmu_send(OP_SYNC_REQ, struct.pack('>I', file_id))
        opcode, payload = self._vmu_recv_check_error()
        if opcode != OP_SYNC_CFM or len(payload) < 6:
            raise DfuError(f"Unexpected sync response: opcode={opcode:#04x}")
        resume_point = payload[0]
        dev_file_id = struct.unpack('>I', payload[1:5])[0]
        proto_ver = payload[5]
        self.log(f"SYNC_CFM: resume={RESUME_NAMES.get(resume_point, resume_point)}, "
                 f"file_id={dev_file_id:#010x}, proto={proto_ver}")
        return resume_point, dev_file_id, proto_ver

    def cmd_abort(self):
        """Send ABORT_REQ, wait for ABORT_CFM."""
        self._vmu_send(OP_ABORT_REQ)
        for _ in range(ABORT_RETRIES):
            try:
                opcode, payload = self._vmu_recv(ABORT_DELAY_MS)
                if opcode == OP_ABORT_CFM:
                    self.log("Abort confirmed")
                    return
            except DfuError:
                pass
            time.sleep(ABORT_DELAY_MS / 1000.0)
        raise DfuError("Abort not confirmed after timeout")

    def cmd_start(self):
        """Send START_REQ, return battery_level."""
        self._vmu_send(OP_START_REQ)
        opcode, payload = self._vmu_recv_check_error()
        if opcode != OP_START_CFM or len(payload) < 3:
            raise DfuError(f"Unexpected start response: opcode={opcode:#04x}")
        status = payload[0]
        battery = struct.unpack('>H', payload[1:3])[0]
        if status != 0x00:
            raise DfuError(f"Start failed with status: {status:#04x}")
        self.log(f"START_CFM: status=OK, battery={battery}")
        return battery

    def cmd_start_data(self):
        """Send START_DATA_REQ."""
        self._vmu_send(OP_START_DATA_REQ)
        self.log(f"Waiting {DATA_REQ_SLEEP_MS}ms for device to prepare...")
        time.sleep(DATA_REQ_SLEEP_MS / 1000.0)

    def write_firmware_data(self, fw_data, progress_cb=None):
        """Transfer firmware data in device-requested chunks."""
        total_size = len(fw_data)
        cur_offset = 0

        while True:
            # Device requests a chunk
            opcode, payload = self._vmu_recv_check_error(timeout_ms=10000)
            if opcode != OP_DATA_BYTES_REQ:
                raise DfuError(f"Expected DATA_BYTES_REQ, got {OPCODE_NAMES.get(opcode, opcode)}")
            if len(payload) < 8:
                raise DfuError(f"DATA_BYTES_REQ payload too short: {len(payload)} bytes (need 8)")

            req_len, req_offset = struct.unpack('>II', payload[:8])
            self.log(f"DATA_BYTES_REQ: len={req_len}, offset={req_offset}")

            if req_len == 0:
                raise DfuError("Device requested 0 bytes")

            cur_offset += req_offset
            if cur_offset + req_len > total_size:
                raise DfuError(f"Request out of bounds: offset={cur_offset}, len={req_len}, total={total_size}")

            is_last_bucket = (cur_offset + req_len >= total_size)
            chunk = fw_data[cur_offset:cur_offset + req_len]

            # Split chunk into HID-sized packets
            # Max data per UPGRADE_DATA packet: DATA_OUT_PAYLOAD_SIZE - 4 (opcode + len + last_packet)
            max_data_per_pkt = DATA_OUT_PAYLOAD_SIZE - 4  # 249 bytes
            offset_in_chunk = 0

            while offset_in_chunk < len(chunk):
                pkt_data = chunk[offset_in_chunk:offset_in_chunk + max_data_per_pkt]
                offset_in_chunk += len(pkt_data)

                is_last_pkt_in_bucket = (offset_in_chunk >= len(chunk))
                is_last = 0x01 if (is_last_bucket and is_last_pkt_in_bucket) else 0x00

                # UPGRADE_DATA: [opcode][data_len BE16][last_packet][firmware data...]
                pkt_payload = struct.pack('>B', is_last) + pkt_data
                self._vmu_send(OP_DATA, pkt_payload)

                time.sleep(SEND_DELAY_MS / 1000.0)

            cur_offset += req_len
            pct = min(100, int(cur_offset * 100 / total_size))
            if progress_cb:
                progress_cb(pct)
            else:
                print(f"\r  Uploading: {pct}% ({cur_offset}/{total_size} bytes)", end='', flush=True)

            if is_last_bucket:
                break

        if not progress_cb:
            print()  # newline after progress

    def cmd_validation(self):
        """Poll for validation completion."""
        for i in range(VALIDATION_RETRIES):
            self._vmu_send(OP_IS_VALIDATION_DONE_REQ)
            # Device-side image verification can legitimately take 40-50 seconds
            # for FMA120 release images (documented in Flairmesh's ReadMe).
            opcode, payload = self._vmu_recv_check_error(timeout_ms=60000)

            if opcode == OP_TRANSFER_COMPLETE_IND:
                self.log("Validation complete!")
                return

            if opcode == OP_IS_VALIDATION_DONE_CFM and len(payload) >= 2:
                delay_ms = struct.unpack('>H', payload[:2])[0]
                self.log(f"Validation in progress, waiting {delay_ms}ms...")
                time.sleep(delay_ms / 1000.0)
            else:
                raise DfuError(f"Unexpected validation response: {OPCODE_NAMES.get(opcode, opcode)}")

        raise DfuError("Validation timed out")

    def cmd_transfer_complete(self):
        """Send TRANSFER_COMPLETE_RES and wait for the HID reboot disconnect."""
        self._vmu_send(OP_TRANSFER_COMPLETE_RES, struct.pack('>B', 0x00))  # Interactive
        try:
            self._recv(timeout_ms=30000)
        except OSError:
            self.log("Device disconnected for reboot")
            return
        except DfuError as exc:
            raise DfuError("Device did not disconnect for reboot within 30 seconds") from exc
        raise DfuError("Unexpected response while waiting for reboot disconnect")

    def cmd_proceed_to_commit(self):
        """Send PROCEED_TO_COMMIT, wait for COMMIT_REQ."""
        self._vmu_send(OP_PROCEED_TO_COMMIT, struct.pack('>B', 0x00))  # Proceed
        opcode, payload = self._vmu_recv_check_error(timeout_ms=10000)
        if opcode != OP_COMMIT_REQ:
            raise DfuError(f"Expected COMMIT_REQ, got {OPCODE_NAMES.get(opcode, opcode)}")
        self.log("COMMIT_REQ received")

    def cmd_commit(self):
        """Send COMMIT_CFM, wait for COMPLETE_IND."""
        self._vmu_send(OP_COMMIT_CFM, struct.pack('>B', 0x00))  # Upgrade (not rollback)
        opcode, payload = self._vmu_recv_check_error(timeout_ms=30000)
        if opcode != OP_COMPLETE_IND:
            raise DfuError(f"Expected COMPLETE_IND, got {OPCODE_NAMES.get(opcode, opcode)}")
        self.log("Upgrade complete!")


def parse_firmware_header(data):
    """Parse firmware file header."""
    if len(data) < 26:
        raise DfuError("Firmware file too small")
    magic = data[:7]
    if magic != b'APPUHDR':
        raise DfuError(f"Invalid firmware magic: {magic}")
    proto_ver = data[7] - 0x30  # ASCII digit to int
    length = struct.unpack('>I', data[8:12])[0]
    variant = data[12:20].rstrip(b'\x00').decode('ascii', errors='replace')
    major = struct.unpack('>H', data[20:22])[0]
    minor = struct.unpack('>H', data[22:24])[0]
    n_parts = struct.unpack('>H', data[24:26])[0]
    file_id = zlib.crc32(data) & 0xFFFFFFFF
    return {
        'protocol_version': proto_ver,
        'header_length': length,
        'variant': variant,
        'major': major,
        'minor': minor,
        'partitions': n_parts,
        'file_id': file_id,
        'size': len(data),
    }


def show_firmware_info(path):
    """Show firmware file information."""
    with open(path, 'rb') as f:
        data = f.read()
    info = parse_firmware_header(data)
    print(f"Firmware: {path}")
    print(f"  Size:       {info['size']} bytes ({info['size']/1024:.1f} KB)")
    print(f"  Version:    {info['major']}.{info['minor']}")
    print(f"  Variant:    {info['variant']}")
    print(f"  Protocol:   {info['protocol_version']}")
    print(f"  Partitions: {info['partitions']}")
    print(f"  File ID:    {info['file_id']:#010x}")


def do_upgrade(fw_path, verbose=False):
    """Perform the complete firmware upgrade."""
    # Read and parse firmware
    with open(fw_path, 'rb') as f:
        fw_data = f.read()
    info = parse_firmware_header(fw_data)
    file_id = info['file_id']
    file_version = info['protocol_version']

    print(f"Firmware: v{info['major']}.{info['minor']} ({info['variant']}), "
          f"{info['size']} bytes, CRC={file_id:#010x}")

    dev = HidDfuDevice(verbose=verbose)
    try:
        # Phase 1: Open and connect
        print("Opening device...")
        dev.open()
        print("Connecting...")
        dev.connect()

        # Phase 2: Sync
        print("Syncing...")
        resume_point, dev_file_id, proto_ver = dev.cmd_sync(file_id)
        print(f"  Resume point: {RESUME_NAMES.get(resume_point, resume_point)}")
        print(f"  Protocol version: {proto_ver}")

        if proto_ver != file_version:
            raise DfuError(f"Protocol version mismatch: device={proto_ver}, firmware={file_version}")

        # If file_id doesn't match (different firmware was partially written), abort first
        if dev_file_id != file_id and resume_point != RESUME_START:
            print("  File ID mismatch - aborting previous upgrade...")
            dev.cmd_abort()
            resume_point, dev_file_id, proto_ver = dev.cmd_sync(file_id)

        # If resuming from Start, abort any stale partial state
        if resume_point == RESUME_START:
            print("  Clearing stale state...")
            dev.cmd_abort()
            resume_point, _, _ = dev.cmd_sync(file_id)

        # Phase 3: Start
        print("Starting upgrade...")
        battery = dev.cmd_start()
        print(f"  Battery level: {battery}")

        # Phase 4: Data transfer (only if starting fresh)
        if resume_point == RESUME_START:
            print("Starting data transfer...")
            dev.cmd_start_data()
            dev.write_firmware_data(fw_data)
            resume_point = RESUME_PRE_VALIDATE

        # Phase 5: Validation
        if resume_point == RESUME_PRE_VALIDATE:
            print("Validating firmware image...")
            dev.cmd_validation()
            resume_point = RESUME_PRE_REBOOT

        # Phase 6: Transfer complete (triggers reboot)
        if resume_point == RESUME_PRE_REBOOT:
            print("Sending transfer complete (device will reboot)...")
            dev.cmd_transfer_complete()

            # Phase 7: Wait for device to reboot and re-enumerate
            print("Waiting for device to reboot...")
            dev.close()
            time.sleep(5)

            # Re-open and reconnect
            for attempt in range(30):
                try:
                    dev.open()
                    break
                except DfuError:
                    time.sleep(1)
                    if attempt % 5 == 4:
                        print(f"  Still waiting... ({attempt+1}s)")
            else:
                raise DfuError("Device did not re-appear after reboot")

            print("Device reconnected. Finalizing...")
            dev.connect()

            # Phase 8: Post-reboot sync and commit
            resume_point, _, _ = dev.cmd_sync(file_id)
            print(f"  Post-reboot resume point: {RESUME_NAMES.get(resume_point, resume_point)}")
            dev.cmd_start()

        if resume_point not in (RESUME_POST_REBOOT, RESUME_COMMIT, RESUME_POST_COMMIT):
            raise DfuError(
                f"Device did not enter a committable state after reboot: "
                f"{RESUME_NAMES.get(resume_point, resume_point)}"
            )

        if resume_point == RESUME_POST_REBOOT:
            dev.cmd_proceed_to_commit()
            resume_point = RESUME_COMMIT

        # Phase 9: Commit
        print("Committing upgrade...")
        dev.cmd_commit()

        # Phase 10: Disconnect
        dev.disconnect()
        print("\nFirmware upgrade complete!")

    except Exception as e:
        print(f"\nERROR: {e}")
        try:
            dev.disconnect()
        except Exception:
            pass
        raise
    finally:
        dev.close()


def do_version(verbose=False):
    """Read and display device firmware version."""
    dev = HidDfuDevice(verbose=verbose)
    try:
        dev.open()
        dev.connect()
        version = dev.cmd_version()
        print(f"Device firmware version: {version}")
        dev.disconnect()
    finally:
        dev.close()


def main():
    parser = argparse.ArgumentParser(description='HID DFU Tool for FMA120 (QCC3086)')
    parser.add_argument('firmware', nargs='?', help='Firmware .bin file to upload')
    parser.add_argument('--version', action='store_true', help='Read device firmware version')
    parser.add_argument('--info', action='store_true', help='Show firmware file info only')
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')
    args = parser.parse_args()

    if args.version:
        do_version(verbose=args.verbose)
    elif args.info and args.firmware:
        show_firmware_info(args.firmware)
    elif args.firmware:
        do_upgrade(args.firmware, verbose=args.verbose)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
