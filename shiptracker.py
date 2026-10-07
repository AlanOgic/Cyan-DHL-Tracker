#!/usr/bin/env python3
import logging
import sys
from dotenv import load_dotenv

from alert_dispatch import AlertConfigError
from dhl_client import DHLConfigError, DHLTracker
from odoo_json2 import OdooConfigError
from shipment_sync import build_from_env

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
    
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    # Initialize clients
    try:
        odoo_client, shipment_sync = build_from_env()
        dhl_tracker = DHLTracker()
    except (OdooConfigError, DHLConfigError, AlertConfigError) as e:
        print(f"[-] Invalid configuration: {e}")
        sys.exit(1)
    if not odoo_client.connect():
        print("[-] Failed to connect to Odoo. Please check your credentials.")
        sys.exit(1)
    
    while True:
        choice = main_menu()
        
        if choice == '1':
            # Track a shipment
            tracking_number = input("\nEnter tracking number: ")
            
            print(f"\n[*] Tracking shipment {tracking_number}...")
            tracking_data = dhl_tracker.track_shipment(tracking_number)
            
            # Try to find partner info (best effort: an Odoo error is already logged)
            partner_info = None
            shipments = odoo_client.get_recent_shipments(limit=100) or []
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

            if shipments is None:
                print("[-] Could not read shipments from Odoo (see the error above).")
            elif not shipments:
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
                    
                    # Get status from DHL, then update Odoo (alerting on a new action code;
                    # transient DHL errors such as rate limits keep the last known Odoo status)
                    result = shipment_sync.apply(shipment, dhl_tracker.track_reference(tracking))
                    status_description, next_steps, is_delivered = result.status, result.next_steps, result.delivered
                    status_display = status_description[:13] + '..' if len(status_description) > 15 else status_description
                    
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
                shipments = odoo_client.get_recent_shipments(limit=100) or []
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
