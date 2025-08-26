#!/usr/bin/env python3
"""
Simple command test for CDC debugging
"""

import json
import serial
import serial.tools.list_ports
import time

def find_cdc_ports():
    """Find CDC ports"""
    cdc_ports = []
    ports = serial.tools.list_ports.comports()
    
    for port in ports:
        desc = port.description.lower()
        if 'usb serial' in desc or 'cdc' in desc or 'com' in port.device.lower():
            cdc_ports.append(port.device)
    
    return cdc_ports

def test_commands():
    """Test various commands to see which ones work"""
    ports = find_cdc_ports()
    if not ports:
        print("No CDC ports found")
        return
    
    print(f"Using port: {ports[-1]}")
    
    try:
        ser = serial.Serial(ports[-1], 115200, timeout=5)
        
        # Test commands one by one
        test_commands = [
            {"commands": [{"command": "get_device_mode"}]},
            {"commands": [{"command": "pause", "data": {"pause": False}}]},
            {"commands": [{"command": "switch_mode", "data": {"mode": "wifi"}}]},
            {"commands": [{"command": "get_serial"}]},
            {"commands": [{"command": "test"}]},  # This should fail
        ]
        
        for i, cmd in enumerate(test_commands):
            print(f"\n=== Test {i+1}: {cmd['commands'][0]['command']} ===")
            
            cmd_json = json.dumps(cmd) + '\n'
            print(f"Sending: {cmd_json.strip()}")
            
            # Clear any previous data
            ser.reset_input_buffer()
            
            # Send command
            ser.write(cmd_json.encode())
            ser.flush()
            
            # Read response with timeout
            response_data = ""
            start_time = time.time()
            while time.time() - start_time < 3:
                if ser.in_waiting > 0:
                    data = ser.read(ser.in_waiting).decode('utf-8', errors='replace')
                    response_data += data
                    if response_data.strip().endswith('}'):
                        break
                time.sleep(0.1)
            
            if response_data:
                print(f"Response: {response_data.strip()}")
                
                # Try to parse as JSON
                try:
                    clean_response = response_data.strip().replace('\n\t', '').replace('\t', ' ')
                    parsed = json.loads(clean_response)
                    print(f"Parsed: {parsed}")
                except json.JSONDecodeError as e:
                    print(f"JSON parse error: {e}")
            else:
                print("No response received")
            
            time.sleep(1)  # Wait between commands
        
        ser.close()
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_commands()