#!/usr/bin/env python3
"""
Simple OpenIris UVC CDC Test - Minimal version to debug JSON parsing
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

def test_cdc_raw():
    """Test CDC with raw response parsing"""
    ports = find_cdc_ports()
    if not ports:
        print("No CDC ports found")
        return
    
    print(f"Using port: {ports[-1]}")
    
    try:
        ser = serial.Serial(ports[-1], 115200, timeout=3)
        
        # Send get_device_mode command
        cmd = '{"commands": [{"command": "get_device_mode"}]}\n'
        print(f"Sending: {cmd.strip()}")
        
        ser.write(cmd.encode())
        ser.flush()
        
        # Read raw response
        print("Raw response:")
        time.sleep(1)
        
        response_data = ""
        start_time = time.time()
        while time.time() - start_time < 5:
            if ser.in_waiting > 0:
                data = ser.read(ser.in_waiting).decode('utf-8', errors='replace')
                response_data += data
                print(f"Received: {repr(data)}")
            time.sleep(0.1)
        
        print(f"\nComplete response: {repr(response_data)}")
        
        # Try to manually parse the response
        lines = response_data.strip().split('\n')
        json_lines = [line.strip() for line in lines if line.strip()]
        
        print(f"JSON lines: {json_lines}")
        
        # Try to reconstruct JSON
        if len(json_lines) >= 3 and json_lines[0] == '{' and json_lines[-1] == '}':
            # Combine into single JSON string
            full_json = ''.join(json_lines)
            print(f"Reconstructed JSON: {full_json}")
            
            try:
                parsed = json.loads(full_json)
                print(f"Parsed successfully: {parsed}")
                
                # Handle the double encoding
                if "results" in parsed and isinstance(parsed["results"], list):
                    for result in parsed["results"]:
                        if isinstance(result, str) and result.startswith('{"result"'):
                            inner = json.loads(result)
                            if "result" in inner:
                                final = json.loads(inner["result"])
                                print(f"Final decoded result: {final}")
                                
            except json.JSONDecodeError as e:
                print(f"JSON parse error: {e}")
        
        ser.close()
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_cdc_raw()