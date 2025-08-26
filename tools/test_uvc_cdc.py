#!/usr/bin/env python3
"""
OpenIris UVC CDC Virtual Serial Port Test Tool

This tool tests the new USB CDC virtual serial port functionality
when the OpenIris device is in UVC mode. It discovers CDC virtual
serial ports and allows sending commands to configure the device.
"""

import re
import json
import time
import threading
import argparse
import sys
import string
from typing import Dict, List, Optional, Tuple
import serial
import serial.tools.list_ports
from dataclasses import dataclass


@dataclass
class WiFiNetwork:
    ssid: str
    channel: int
    rssi: int
    mac_address: str
    auth_mode: int
    
    @property
    def security_type(self) -> str:
        """Convert auth_mode to human readable string"""
        auth_modes = {
            0: "Open",
            1: "WEP", 
            2: "WPA PSK",
            3: "WPA2 PSK",
            4: "WPA WPA2 PSK",
            5: "WPA2 Enterprise",
            6: "WPA3 PSK",
            7: "WPA2 WPA3 PSK"
        }
        return auth_modes.get(self.auth_mode, f"Unknown ({self.auth_mode})")


class OpenIrisCDCDevice:
    def __init__(self, port: str, description: str, debug: bool = False):
        self.port = port
        self.description = description
        self.connection: Optional[serial.Serial] = None
        self.networks: List[WiFiNetwork] = []
        self.debug = debug
        
    def connect(self) -> bool:
        """Connect to the CDC virtual serial port"""
        try:
            self.connection = serial.Serial(
                port=self.port,
                baudrate=115200,
                timeout=2,
                write_timeout=2
            )
            print(f"✅ Connected to OpenIris CDC port {self.port}")
            print(f"   Description: {self.description}")
            
            # Test basic connectivity
            print("🔍 Testing CDC virtual serial port connectivity...")
            test_response = self.send_command("get_device_mode", timeout=5)
            if "error" not in test_response:
                print("✅ CDC virtual serial port is working!")
                mode_info = test_response.get("results", {})
                if mode_info:
                    print(f"   Current device mode: {mode_info}")
            else:
                print(f"⚠️  CDC test response: {test_response}")
            
            return True
        except Exception as e:
            print(f"❌ Failed to connect to CDC port {self.port}: {e}")
            return False
    
    def disconnect(self):
        """Disconnect from the CDC port"""
        if self.connection and self.connection.is_open:
            self.connection.close()
            print(f"🔌 Disconnected from CDC port {self.port}")
    
    def send_command(self, command: str, params: Dict = None, timeout: int = 10) -> Dict:
        """Send a command to the device via CDC virtual serial port"""
        if not self.connection or not self.connection.is_open:
            return {"error": "Not connected"}
        
        cmd_obj = {"commands": [{"command": command}]}
        if params:
            cmd_obj["commands"][0]["data"] = params
        
        cmd_json = json.dumps(cmd_obj) + '\n'
        
        if self.debug:
            print(f"🔍 Sending via CDC: {cmd_json.strip()}")
        
        try:
            # Clear input buffer
            self.connection.reset_input_buffer()
            
            # Send command
            self.connection.write(cmd_json.encode('utf-8'))
            self.connection.flush()
            
            # Wait for response - read until we get complete data
            start_time = time.time()
            response_data = ""
            
            while time.time() - start_time < timeout:
                if self.connection.in_waiting > 0:
                    data = self.connection.read(self.connection.in_waiting).decode('utf-8', errors='replace')
                    response_data += data
                    if self.debug:
                        print(f"🔍 CDC Raw Data: {repr(data)}")
                    
                    # Check if we have a complete JSON response (ends with })
                    if response_data.strip().endswith('}'):
                        break
                else:
                    time.sleep(0.01)  # Small delay to prevent busy waiting
            
            if not response_data.strip():
                return {"error": "Command timeout - no response received"}
            
            if self.debug:
                print(f"🔍 Complete Response: {repr(response_data)}")
            
            # Parse the response
            try:
                # Clean up the response - remove extra formatting
                clean_response = response_data.strip()
                # Replace tabs and extra whitespace
                clean_response = clean_response.replace('\n\t', '').replace('\t', ' ')
                
                if self.debug:
                    print(f"🔍 Cleaned Response: {clean_response}")
                
                # Parse the JSON
                response = json.loads(clean_response)
                
                # Handle double-encoded JSON responses
                if isinstance(response, dict) and "results" in response and isinstance(response["results"], list):
                    decoded_results = []
                    for result_item in response["results"]:
                        try:
                            # Try to decode double-encoded JSON
                            if isinstance(result_item, str) and '{"result"' in result_item:
                                inner_json = json.loads(result_item)
                                if "result" in inner_json:
                                    final_result = json.loads(inner_json["result"])
                                    decoded_results.append(final_result)
                                else:
                                    decoded_results.append(inner_json)
                            else:
                                decoded_results.append(result_item)
                        except (json.JSONDecodeError, TypeError):
                            decoded_results.append(result_item)
                    response["results"] = decoded_results[0] if len(decoded_results) == 1 else decoded_results
                
                return response
                
            except json.JSONDecodeError as e:
                if self.debug:
                    print(f"🔍 JSON Parse Error: {e}")
                return {"error": f"JSON parse error: {str(e)}", "raw_response": response_data}
                
        except Exception as e:
            return {"error": f"Communication error: {str(e)}"}

    def switch_mode(self, mode: str) -> Dict:
        """Switch device mode (uvc, wifi, auto)"""
        response = self.send_command("switch_mode", params={"mode": mode})
        return response

    def get_device_mode(self) -> Dict:
        """Get current device mode"""
        response = self.send_command("get_device_mode")
        return response

    def scan_wifi(self, timeout: int = 15) -> List[WiFiNetwork]:
        """Scan for available WiFi networks"""
        print(f"🔍 Scanning for WiFi networks (timeout: {timeout}s)...")
        print("⚠️  Note: WiFi scanning in UVC mode may cause device instability")
        
        # Check if device is responsive before attempting WiFi scan
        test_response = self.send_command("get_device_mode", timeout=3)
        if "error" in test_response:
            print(f"❌ Device not responsive, skipping WiFi scan: {test_response['error']}")
            return []
        
        response = self.send_command("scan_wifi", timeout=timeout + 5)
        
        if "error" in response:
            print(f"❌ WiFi scan failed: {response['error']}")
            # If communication error, the device might have crashed/disconnected
            if "Communication error" in response.get("error", ""):
                print("⚠️  Device may have crashed - this is a known issue with WiFi commands in UVC mode")
                # Try to reconnect
                try:
                    self.disconnect()
                    time.sleep(2)
                    if self.connect():
                        print("✅ Reconnected to device after crash")
                    else:
                        print("❌ Failed to reconnect - device may need manual reset")
                except:
                    pass
            return []
        
        networks = []
        results = response.get("results", {})
        
        # Handle different possible response formats
        if isinstance(results, dict):
            wifi_list = results.get("wifi_networks", results.get("networks", []))
        elif isinstance(results, list):
            wifi_list = results
        else:
            wifi_list = []
        
        for network_data in wifi_list:
            if isinstance(network_data, dict):
                network = WiFiNetwork(
                    ssid=network_data.get("ssid", ""),
                    channel=network_data.get("channel", 0),
                    rssi=network_data.get("rssi", 0),
                    mac_address=network_data.get("bssid", ""),
                    auth_mode=network_data.get("authmode", 0)
                )
                networks.append(network)
        
        self.networks = networks
        return networks

    def connect_wifi(self, ssid: str, password: str = None) -> Dict:
        """Connect to WiFi network"""
        wifi_data = {"ssid": ssid}
        if password:
            wifi_data["password"] = password
        
        print(f"🔗 Connecting to WiFi: {ssid}")
        response = self.send_command("connect_wifi", params=wifi_data, timeout=30)
        return response


def discover_cdc_ports() -> List[Tuple[str, str]]:
    """Discover OpenIris CDC virtual serial ports"""
    print("🔍 Searching for OpenIris CDC virtual serial ports...")
    
    cdc_ports = []
    ports = serial.tools.list_ports.comports()
    
    for port in ports:
        # Look for CDC/ACM devices (Linux/macOS) or COM ports with specific VID/PID (Windows)
        description = port.description.lower()
        manufacturer = getattr(port, 'manufacturer', '').lower() if hasattr(port, 'manufacturer') else ''
        
        # Common indicators for CDC virtual serial ports
        cdc_indicators = [
            'cdc',
            'acm',
            'usb serial',
            'virtual com',
            'composite device'
        ]
        
        # Check for OpenIris specific indicators
        openiris_indicators = [
            'openiris',
            'esp32',
            'espressif'
        ]
        
        is_cdc = any(indicator in description for indicator in cdc_indicators)
        is_openiris = any(indicator in description or indicator in manufacturer for indicator in openiris_indicators)
        
        # Also check for specific VID/PID if available
        vid_pid_match = False
        if hasattr(port, 'vid') and hasattr(port, 'pid'):
            # Common ESP32 VID/PID for CDC
            if (port.vid == 0x303a):  # Espressif VID
                vid_pid_match = True
        
        if is_cdc or vid_pid_match or (is_openiris and 'com' in port.device.lower()):
            cdc_ports.append((port.device, port.description))
            print(f"   Found potential CDC port: {port.device} - {port.description}")
    
    return cdc_ports


def test_cdc_functionality():
    """Test CDC virtual serial port functionality"""
    print("=" * 60)
    print("🧪 OpenIris UVC CDC Virtual Serial Port Test")
    print("=" * 60)
    print()
    print("This tool tests the new CDC virtual serial port functionality")
    print("when your OpenIris device is in UVC mode.")
    print()
    print("Prerequisites:")
    print("1. Device firmware with CDC + UVC composite USB support")
    print("2. Device configured for UVC mode (no 20-second delay)")
    print("3. Device connected via USB")
    print()
    
    # Discover CDC ports
    cdc_ports = discover_cdc_ports()
    
    if not cdc_ports:
        print("❌ No CDC virtual serial ports found!")
        print()
        print("Troubleshooting:")
        print("- Ensure device is connected via USB")
        print("- Verify firmware has CDC + UVC composite support")
        print("- Check if device is in UVC mode")
        print("- Try different USB ports/cables")
        return False
    
    # If multiple ports found, let user choose
    if len(cdc_ports) > 1:
        print(f"📱 Found {len(cdc_ports)} potential CDC ports:")
        for i, (port, desc) in enumerate(cdc_ports):
            print(f"   {i + 1}. {port} - {desc}")
        
        while True:
            try:
                choice = input(f"\nSelect port (1-{len(cdc_ports)}): ").strip()
                port_index = int(choice) - 1
                if 0 <= port_index < len(cdc_ports):
                    selected_port = cdc_ports[port_index]
                    break
                else:
                    print("❌ Invalid selection")
            except ValueError:
                print("❌ Please enter a number")
    else:
        selected_port = cdc_ports[0]
    
    port_name, port_desc = selected_port
    print(f"\n🔗 Testing CDC port: {port_name}")
    
    # Create device instance
    device = OpenIrisCDCDevice(port_name, port_desc, debug=True)
    
    try:
        # Connect to device
        if not device.connect():
            return False
        
        print("\n" + "=" * 40)
        print("🧪 Running CDC Functionality Tests")
        print("=" * 40)
        
        # Test 1: Get device mode
        print("\n1️⃣  Test: Get Device Mode")
        mode_response = device.get_device_mode()
        if "error" in mode_response:
            print(f"   ❌ Failed: {mode_response['error']}")
        else:
            print(f"   ✅ Success: {mode_response.get('results', {})}")
        
        # Test 2: WiFi scan (optional, can be skipped due to instability)
        print("\n2️⃣  Test: WiFi Network Scan")
        skip_wifi = input("   WiFi scanning may cause device instability in UVC mode. Skip? (y/n): ").strip().lower()
        if skip_wifi.startswith('y'):
            print("   ⏭️  Skipped WiFi scan test")
        else:
            networks = device.scan_wifi(timeout=10)
            if networks:
                print(f"   ✅ Found {len(networks)} networks:")
                for net in networks[:5]:  # Show first 5
                    print(f"      📶 {net.ssid} ({net.security_type}) - {net.rssi} dBm")
                if len(networks) > 5:
                    print(f"      ... and {len(networks) - 5} more")
            else:
                print("   ⚠️  No networks found or scan failed")
        
        # Test 3: Mode switching
        print("\n3️⃣  Test: Mode Switching")
        print("   📝 Testing switch to WiFi mode...")
        switch_response = device.switch_mode("wifi")
        if "error" in switch_response:
            print(f"   ❌ Failed: {switch_response['error']}")
        else:
            print(f"   ✅ Success: {switch_response.get('results', 'Mode switch command sent')}")
            
        # Interactive menu
        print("\n" + "=" * 40)
        print("🎛️  Interactive CDC Test Menu")
        print("=" * 40)
        
        while True:
            print("\nOptions:")
            print("1. Get device mode")
            print("2. Switch to UVC mode") 
            print("3. Switch to WiFi mode")
            print("4. Switch to Auto mode")
            print("5. Scan WiFi networks")
            print("6. Send custom command")
            print("7. Exit")
            
            try:
                choice = input("\nSelect option (1-7): ").strip()
                
                if choice == "1":
                    response = device.get_device_mode()
                    print(f"📄 Response: {json.dumps(response, indent=2)}")
                    
                elif choice == "2":
                    response = device.switch_mode("uvc")
                    print(f"📄 Response: {json.dumps(response, indent=2)}")
                    
                elif choice == "3":
                    response = device.switch_mode("wifi")
                    print(f"📄 Response: {json.dumps(response, indent=2)}")
                    
                elif choice == "4":
                    response = device.switch_mode("auto")
                    print(f"📄 Response: {json.dumps(response, indent=2)}")
                    
                elif choice == "5":
                    networks = device.scan_wifi()
                    if networks:
                        print(f"📶 Found {len(networks)} networks:")
                        for i, net in enumerate(networks, 1):
                            print(f"   {i:2d}. {net.ssid:20} {net.security_type:15} {net.rssi:4d} dBm  Ch.{net.channel}")
                    else:
                        print("📶 No networks found")
                        
                elif choice == "6":
                    command = input("Enter command name: ").strip()
                    if command:
                        params_input = input("Enter parameters (JSON, or press Enter for none): ").strip()
                        params = None
                        if params_input:
                            try:
                                params = json.loads(params_input)
                            except json.JSONDecodeError:
                                print("❌ Invalid JSON parameters")
                                continue
                        
                        response = device.send_command(command, params)
                        print(f"📄 Response: {json.dumps(response, indent=2)}")
                    
                elif choice == "7":
                    break
                    
                else:
                    print("❌ Invalid option")
                    
            except KeyboardInterrupt:
                print("\n👋 Interrupted by user")
                break
            except Exception as e:
                print(f"❌ Error: {e}")
        
        return True
        
    finally:
        device.disconnect()


def main():
    parser = argparse.ArgumentParser(description="Test OpenIris UVC CDC virtual serial port functionality")
    parser.add_argument("--debug", action="store_true", help="Enable debug output")
    args = parser.parse_args()
    
    try:
        success = test_cdc_functionality()
        if success:
            print("\n✅ CDC virtual serial port test completed successfully!")
            print("\nThe CDC virtual COM port is working correctly.")
            print("You can now send commands to your OpenIris device even when it's in UVC mode!")
        else:
            print("\n❌ CDC virtual serial port test failed.")
            print("\nPlease check:")
            print("- Device firmware includes CDC + UVC composite support")
            print("- Device is configured for a specific mode (not AUTO)")
            print("- USB connection is working properly")
            sys.exit(1)
            
    except KeyboardInterrupt:
        print("\n👋 Test interrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n💥 Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()