#!/usr/bin/env python3
import os
import json
import requests
import xmlrpc.client
import sys
import time
from dotenv import load_dotenv
from datetime import datetime

# Load environment variables
load_dotenv()

# ASCII Art Title
TITLE = r"""
  ____                   _____                _             
 / ___|   _  __ _ _ __   |_   _| __ __ _  ___| | _____ _ __ 
| |  | | | |/ _` | '_ \    | || '__/ _` |/ __| |/ / _ \ '__|
| |__| |_| | (_| | | | |   | || | | (_| | (__|   <  __/ |   
 \____\__, |\__,_|_| |_|   |_||_|  \__,_|\___|_|\_\___|_|   
      |___/                                                
by Alan, for Cyanview
"""

class OdooClient:
    def __init__(self):
        self.url = os.getenv('ODOO_URL')
        self.db = os.getenv('ODOO_DB')
        self.username = os.getenv('ODOO_USERNAME')
        self.password = os.getenv('ODOO_PASSWORD')
        self.common = None
        self.uid = None
        self.models = None
    
    def connect(self):
        """Connect to the Odoo instance"""
        print("[*] Connecting to Odoo...")
        try:
            import ssl
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            
            self.common = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/common', context=context)
            self.uid = self.common.authenticate(self.db, self.username, self.password, {})
            self.models = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/object', context=context)
            print("[+] Connection successful!")
            return True
        except Exception as e:
            print(f"[-] Connection failed: {str(e)}")
            return False
    
    def get_partner_info(self, partner_id=None, name=None):
        """
        Get partner information by ID or name
        
        Args:
            partner_id: The Odoo partner ID
            name: The name to search for
            
        Returns:
            Dictionary containing partner info
        """
        domain = []
        if partner_id:
            domain.append(('id', '=', partner_id))
        elif name:
            domain.append(('name', 'ilike', name))
        else:
            return None
        
        try:
            partners = self.models.execute_kw(
                self.db, self.uid, self.password,
                'res.partner', 'search_read',
                [domain],
                {
                    'fields': ['name', 'email', 'phone', 'street', 'city', 'zip', 'country_id'],
                    'limit': 1
                }
            )
            
            if partners:
                partner = partners[0]
                # Format country
                if partner.get('country_id'):
                    partner['country'] = partner['country_id'][1] if isinstance(partner['country_id'], list) else "Unknown"
                
                return partner
            return None
        except Exception as e:
            print(f"[-] Error fetching partner info: {str(e)}")
            return None
    
    def get_recent_shipments(self, limit=20):
        """
        Fetches recent shipments with tracking numbers from Odoo.
        
        Returns a list of dictionaries containing:
        - tracking_number: The DHL tracking number
        - partner_id: The ID of the partner (customer)
        - partner_name: The name of the partner
        """
        try:
            # Fetch recent shipments with tracking numbers, excluding already delivered ones
            shipments = self.models.execute_kw(
                self.db, self.uid, self.password,
                'stock.picking', 'search_read',
                [
                    [
                        ('carrier_tracking_ref', '!=', False),
                        ('carrier_id.name', 'ilike', 'DHL'),
                        ('state', '=', 'done'),
                        ('x_studio_delivered_', '!=', 'YES')
                    ]
                ],
                {
                    'fields': ['carrier_tracking_ref', 'partner_id', 'name', 'date_done'],
                    'limit': limit,
                    'order': 'date_done desc'
                }
            )
            
            result = []
            for shipment in shipments:
                tracking_number = shipment['carrier_tracking_ref']
                partner_id = shipment['partner_id'][0] if isinstance(shipment['partner_id'], list) else shipment['partner_id']
                partner_name = shipment['partner_id'][1] if isinstance(shipment['partner_id'], list) else "Unknown"
                
                result.append({
                    'tracking_number': tracking_number,
                    'partner_id': partner_id,
                    'partner_name': partner_name,
                    'shipment_ref': shipment['name'],
                    'date_done': shipment['date_done']
                })
            
            return result
        except Exception as e:
            print(f"[-] Error fetching shipments: {str(e)}")
            return []
    
    def update_delivery_status(self, tracking_number, delivered=True, current_status=None, next_steps=None):
        """
        Updates the delivery status in Odoo stock.picking model.
        
        Args:
            tracking_number: The DHL tracking number
            delivered: Boolean indicating if the shipment is delivered
            current_status: Current status description for non-delivered shipments
            next_steps: Next steps description for non-delivered shipments
            
        Returns:
            Boolean indicating success or failure
        """
        try:
            # Find the stock.picking record with this tracking number
            picking_ids = self.models.execute_kw(
                self.db, self.uid, self.password,
                'stock.picking', 'search',
                [
                    [
                        ('carrier_tracking_ref', '=', tracking_number),
                        ('carrier_id.name', 'ilike', 'DHL')
                    ]
                ]
            )
            
            if not picking_ids:
                print(f"[-] No stock.picking record found for tracking number {tracking_number}")
                return False
            
            # Update fields based on delivery status
            if delivered:
                # Set delivered to YES and clear status field
                update_result = self.models.execute_kw(
                    self.db, self.uid, self.password,
                    'stock.picking', 'write',
                    [picking_ids, {'x_studio_delivered_': 'YES', 'x_studio_status': ''}]
                )
            else:
                # For non-delivered: update status field with current status + next steps
                status_text = ""
                if current_status:
                    status_text += current_status
                if next_steps:
                    if status_text:
                        status_text += " | " + next_steps
                    else:
                        status_text = next_steps
                
                update_result = self.models.execute_kw(
                    self.db, self.uid, self.password,
                    'stock.picking', 'write',
                    [picking_ids, {'x_studio_status': status_text}]
                )
                
                if update_result:
                    print(f"[+] Updated status for tracking {tracking_number}: {status_text}")
                    return True
            
            if update_result:
                print(f"[+] Updated delivery status for tracking {tracking_number}: YES")
                return True
            else:
                print(f"[-] Failed to update delivery status for tracking {tracking_number}")
                return False
                
        except Exception as e:
            print(f"[-] Error updating delivery status for {tracking_number}: {str(e)}")
            return False

class DHLTracker:
    def __init__(self):
        self.api_key = os.getenv('DHL_API_KEY')
        self.base_url = "https://api-eu.dhl.com/track/shipments"
    
    def track_shipment(self, tracking_number):
        """
        Tracks a DHL shipment using the DHL Tracking API.
        
        Args:
            tracking_number: The DHL tracking number
            
        Returns:
            Dictionary containing the tracking information
        """
        headers = {
            "DHL-API-Key": self.api_key,
            "Accept": "application/json"
        }
        
        params = {
            "trackingNumber": tracking_number
        }
        
        response = requests.get(self.base_url, headers=headers, params=params)
        time.sleep(5)  # Rate limiting: wait 5 seconds between requests (DHL API limit)
        
        if response.status_code == 200:
            return response.json()
        else:
            return {
                "error": True,
                "status_code": response.status_code,
                "message": response.text
            }
    
    def get_shipment_status(self, tracking_number):
        """
        Get just the current status of a shipment.
        
        Args:
            tracking_number: The DHL tracking number
            
        Returns:
            Tuple containing (status_description, next_steps, is_delivered)
        """
        tracking_data = self.track_shipment(tracking_number)
        
        if tracking_data.get("error"):
            # Debug: print error details for troubleshooting
            status_code = tracking_data.get("status_code")
            if status_code == 404:
                return ("Not Found", None, False)
            elif status_code == 401:
                return ("Auth Error", None, False)
            elif status_code == 429:
                return ("Rate Limited", None, False)
            else:
                return (f"Error {status_code}", None, False)
        
        if "shipments" in tracking_data and tracking_data["shipments"]:
            shipment = tracking_data["shipments"][0]
            if "status" in shipment:
                status_info = shipment["status"]
                
                # Get current status description
                description = status_info.get("description", status_info.get("status", "Unknown"))
                
                # Check if delivered
                status_code = status_info.get("statusCode", "").lower()
                is_delivered = "delivered" in description.lower() or status_code in ["delivered", "ok"]
                
                # Get next steps for non-delivered shipments
                next_steps = None if is_delivered else status_info.get("nextSteps")
                
                return (description, next_steps, is_delivered)
        
        return ("No data", None, False)

def display_tracking_info(tracking_data, partner_info=None):
    """
    Display tracking information in a formatted way
    """
    if "error" in tracking_data:
        print(f"\n[-] Error tracking shipment: {tracking_data.get('message', 'Unknown error')}")
        return
    
    if "shipments" not in tracking_data or not tracking_data["shipments"]:
        print("\n[-] No shipment data found")
        return
    
    shipment = tracking_data["shipments"][0]
    
    # Display partner info if available
    if partner_info:
        print("\n" + "=" * 50)
        print(f"PARTNER: {partner_info.get('name', 'Unknown')}")
        print(f"Email: {partner_info.get('email', 'N/A')}")
        print(f"Phone: {partner_info.get('phone', 'N/A')}")
        address = []
        if partner_info.get('street'):
            address.append(partner_info['street'])
        if partner_info.get('city'):
            address.append(partner_info['city'])
        if partner_info.get('zip'):
            address.append(partner_info['zip'])
        if partner_info.get('country'):
            address.append(partner_info['country'])
        
        print(f"Address: {', '.join(address)}")
        print("=" * 50)
    
    # Basic shipment information
    print(f"\nTRACKING NUMBER: {shipment.get('id', 'Unknown')}")
    print(f"Service: {shipment.get('service', 'Unknown')}")
    
    # Status information
    status = shipment.get("status", {})
    print(f"\nCURRENT STATUS: {status.get('status', 'Unknown')} ({status.get('statusCode', 'Unknown')})")
    print(f"Timestamp: {status.get('timestamp', 'Unknown')}")
    
    if status.get('location') and status['location'].get('address'):
        location = status['location']['address']
        loc_parts = []
        if location.get('addressLocality'):
            loc_parts.append(location['addressLocality'])
        if location.get('postalCode'):
            loc_parts.append(location['postalCode'])
        if location.get('countryCode'):
            loc_parts.append(location['countryCode'])
        
        if loc_parts:
            print(f"Location: {', '.join(loc_parts)}")
    
    if status.get('description'):
        print(f"Description: {status['description']}")
    
    # Next steps
    if status.get('nextSteps'):
        print(f"\nNEXT STEPS: {status['nextSteps']}")
    
    # Estimated delivery
    if shipment.get('estimatedTimeOfDelivery'):
        print(f"\nESTIMATED DELIVERY: {shipment['estimatedTimeOfDelivery']}")
    
    # Show events
    if "events" in shipment and shipment["events"]:
        print("\nSHIPMENT HISTORY:")
        print("-" * 80)
        for event in shipment["events"]:
            timestamp = event.get('timestamp', 'Unknown time')
            status_code = event.get('statusCode', 'unknown')
            status = event.get('status', 'Unknown status')
            
            location = "Unknown location"
            if event.get('location') and event['location'].get('address'):
                loc = event['location']['address']
                loc_parts = []
                if loc.get('addressLocality'):
                    loc_parts.append(loc['addressLocality'])
                if loc.get('countryCode'):
                    loc_parts.append(loc['countryCode'])
                
                if loc_parts:
                    location = ', '.join(loc_parts)
            
            print(f"{timestamp} | {status_code.upper()} | {status} | {location}")
    
    print("\n" + "=" * 80)

def main_menu():
    """Display the main menu and get user choice"""
    print("\n" + "=" * 50)
    print("MAIN MENU")
    print("=" * 50)
    print("1. Track a shipment")
    print("2. View recent shipments")
    print("3. Get partner information")
    print("4. Exit")
    
    choice = input("\nEnter your choice (1-4): ")
    return choice

def main():
    print(TITLE)
    print("Welcome to ShipTracker - DHL Shipment Tracking System")
    print("=" * 80)
    
    # Initialize clients
    odoo_client = OdooClient()
    if not odoo_client.connect():
        print("[-] Failed to connect to Odoo. Please check your credentials.")
        sys.exit(1)
    
    dhl_tracker = DHLTracker()
    
    while True:
        choice = main_menu()
        
        if choice == '1':
            # Track a shipment
            tracking_number = input("\nEnter tracking number: ")
            
            print(f"\n[*] Tracking shipment {tracking_number}...")
            tracking_data = dhl_tracker.track_shipment(tracking_number)
            
            # Try to find partner info
            partner_info = None
            shipments = odoo_client.get_recent_shipments(limit=100)
            for shipment in shipments:
                if shipment['tracking_number'] == tracking_number:
                    partner_info = odoo_client.get_partner_info(partner_id=shipment['partner_id'])
                    break
            
            display_tracking_info(tracking_data, partner_info)
            input("\nPress Enter to continue...")
        
        elif choice == '2':
            # View recent shipments
            limit = input("\nEnter number of shipments to display (default: 10): ")
            limit = int(limit) if limit.isdigit() else 10
            
            print(f"\n[*] Fetching {limit} recent shipments from Odoo...")
            shipments = odoo_client.get_recent_shipments(limit=limit)
            
            if not shipments:
                print("[-] No shipments found with tracking numbers.")
            else:
                print(f"\n[+] Found {len(shipments)} shipments with tracking numbers.")
                print("[-] Fetching shipment statuses...")
                print("\n" + "=" * 125)
                print(f"{'#':<3} | {'TRACKING NUMBER':<20} | {'PARTNER':<25} | {'REFERENCE':<15} | {'DATE':<12}   | {'STATUS':<15}")
                print("=" * 125)
                
                for idx, shipment in enumerate(shipments, 1):
                    tracking = shipment['tracking_number']
                    partner = shipment['partner_name'][:23] + '..' if len(shipment['partner_name']) > 25 else shipment['partner_name']
                    reference = shipment['shipment_ref']
                    date = shipment['date_done'].split('T')[0] if 'T' in shipment['date_done'] else shipment['date_done']
                    
                    # Get status from DHL API
                    status_description, next_steps, is_delivered = dhl_tracker.get_shipment_status(tracking)
                    status_display = status_description[:13] + '..' if len(status_description) > 15 else status_description
                    
                    # Update Odoo based on delivery status
                    if is_delivered:
                        odoo_client.update_delivery_status(tracking, delivered=True)
                    else:
                        # Update status field for non-delivered shipments
                        odoo_client.update_delivery_status(tracking, delivered=False, 
                                                         current_status=status_description, 
                                                         next_steps=next_steps)
                    
                    print(f"{idx:<3} | {tracking:<20} | {partner:<25} | {reference:<15} | {date:<12} | {status_display:<15}")
                    
                    # Add next steps on a nested line for non-delivered shipments
                    if next_steps and not is_delivered:
                        next_steps_display = next_steps[:100] + '..' if len(next_steps) > 100 else next_steps
                        print(f"    {'└─ Next:':<25} {next_steps_display}")
                        print()  # Add blank line for readability
            
            track_choice = input("\nDo you want to track a shipment from this list? (y/n): ")
            if track_choice.lower() == 'y':
                choice_input = input("Enter list number (e.g., 1, 2, 3) or tracking number: ")
                
                # Check if input is a number (list index)
                if choice_input.isdigit():
                    list_num = int(choice_input)
                    if 1 <= list_num <= len(shipments):
                        selected_shipment = shipments[list_num - 1]
                        tracking_number = selected_shipment['tracking_number']
                        partner_info = odoo_client.get_partner_info(partner_id=selected_shipment['partner_id'])
                    else:
                        print(f"[-] Invalid list number. Please enter a number between 1 and {len(shipments)}")
                        input("\nPress Enter to continue...")
                        continue
                else:
                    # Input is a tracking number
                    tracking_number = choice_input
                    # Find partner info
                    partner_info = None
                    for shipment in shipments:
                        if shipment['tracking_number'] == tracking_number:
                            partner_info = odoo_client.get_partner_info(partner_id=shipment['partner_id'])
                            break
                
                print(f"\n[*] Tracking shipment {tracking_number}...")
                tracking_data = dhl_tracker.track_shipment(tracking_number)
                display_tracking_info(tracking_data, partner_info)
            
            input("\nPress Enter to continue...")
        
        elif choice == '3':
            # Get partner information
            search_by = input("\nSearch by ID or name? (id/name): ")
            
            if search_by.lower() == 'id':
                partner_id = input("Enter partner ID: ")
                if not partner_id.isdigit():
                    print("[-] Partner ID must be a number.")
                    continue
                
                partner_info = odoo_client.get_partner_info(partner_id=int(partner_id))
            else:
                name = input("Enter partner name: ")
                partner_info = odoo_client.get_partner_info(name=name)
            
            if partner_info:
                print("\n" + "=" * 50)
                print(f"PARTNER: {partner_info.get('name', 'Unknown')}")
                print(f"ID: {partner_info.get('id', 'Unknown')}")
                print(f"Email: {partner_info.get('email', 'N/A')}")
                print(f"Phone: {partner_info.get('phone', 'N/A')}")
                address = []
                if partner_info.get('street'):
                    address.append(partner_info['street'])
                if partner_info.get('city'):
                    address.append(partner_info['city'])
                if partner_info.get('zip'):
                    address.append(partner_info['zip'])
                if partner_info.get('country'):
                    address.append(partner_info['country'])
                
                print(f"Address: {', '.join(address)}")
                print("=" * 50)
                
                # Show recent shipments for this partner
                shipments = odoo_client.get_recent_shipments(limit=100)
                partner_shipments = [s for s in shipments if s['partner_id'] == partner_info['id']]
                
                if partner_shipments:
                    print(f"\nRecent shipments for {partner_info['name']}:")
                    print("\n" + "=" * 80)
                    print(f"{'TRACKING NUMBER':<20} | {'REFERENCE':<20} | {'DATE':<20}")
                    print("=" * 80)
                    
                    for shipment in partner_shipments:
                        tracking = shipment['tracking_number']
                        reference = shipment['shipment_ref']
                        date = shipment['date_done'].split('T')[0] if 'T' in shipment['date_done'] else shipment['date_done']
                        
                        print(f"{tracking:<20} | {reference:<20} | {date:<20}")
                    
                    track_choice = input("\nDo you want to track a shipment from this list? (y/n): ")
                    if track_choice.lower() == 'y':
                        tracking_number = input("Enter tracking number: ")
                        print(f"\n[*] Tracking shipment {tracking_number}...")
                        tracking_data = dhl_tracker.track_shipment(tracking_number)
                        display_tracking_info(tracking_data, partner_info)
                else:
                    print("\n[-] No recent shipments found for this partner.")
            else:
                print("\n[-] Partner not found.")
            
            input("\nPress Enter to continue...")
        
        elif choice == '4':
            # Exit
            print("\nThank you for using ShipTracker. Goodbye!")
            sys.exit(0)
        
        else:
            print("\n[-] Invalid choice. Please try again.")

if __name__ == "__main__":
    main()
