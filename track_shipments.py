#!/usr/bin/env python3
import json
import logging
import sys
from dotenv import load_dotenv
from datetime import datetime

from alert_dispatch import AlertConfigError
from dhl_client import DHLConfigError, DHLTracker
from odoo_json2 import OdooConfigError
from shipment_sync import build_from_env

# Load environment variables
load_dotenv()

def process_shipment_data(odoo_data, tracking_data):
    """
    Processes the shipment data from Odoo and DHL tracking API.
    
    Args:
        odoo_data: Dictionary containing Odoo shipment data
        tracking_data: Dictionary containing DHL tracking data
        
    Returns:
        Dictionary containing processed data with partner info and tracking details
    """
    result = {
        "partner": {
            "id": odoo_data["partner_id"],
            "name": odoo_data["partner_name"]
        },
        "shipment": {
            "reference": odoo_data["shipment_ref"],
            "date_done": odoo_data["date_done"],
            "tracking_number": odoo_data["tracking_number"]
        }
    }
    
    # Check if there was an error with the tracking
    if tracking_data.get("error"):
        result["tracking"] = {
            "error": tracking_data.get("message", "Unknown error"),
            "status_code": tracking_data.get("status_code")
        }
        return result
    
    # Process shipment data from DHL response
    if "shipments" in tracking_data and tracking_data["shipments"]:
        shipment = tracking_data["shipments"][0]
        
        # Basic shipment information
        result["tracking"] = {
            "id": shipment.get("id"),
            "service": shipment.get("service"),
            "status": {
                "code": shipment.get("status", {}).get("statusCode"),
                "description": shipment.get("status", {}).get("description"),
                "timestamp": shipment.get("status", {}).get("timestamp"),
                "location": shipment.get("status", {}).get("location", {}).get("address", {})
            },
            "estimated_delivery": shipment.get("estimatedTimeOfDelivery"),
            "next_steps": shipment.get("status", {}).get("nextSteps")
        }
        
        # Origin and destination
        if "origin" in shipment:
            result["tracking"]["origin"] = shipment["origin"]
        
        if "destination" in shipment:
            result["tracking"]["destination"] = shipment["destination"]
        
        # Detailed events
        if "events" in shipment:
            result["tracking"]["events"] = [
                {
                    "timestamp": event.get("timestamp"),
                    "status": event.get("status"),
                    "status_code": event.get("statusCode"),
                    "description": event.get("description"),
                    "location": event.get("location", {}).get("address", {})
                }
                for event in shipment["events"]
            ]
    else:
        result["tracking"] = {
            "error": "No shipment data found"
        }
    
    return result

def main():
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    # Initialize clients
    try:
        odoo_client, shipment_sync = build_from_env()
        dhl_tracker = DHLTracker()
    except (OdooConfigError, DHLConfigError, AlertConfigError) as e:
        sys.exit(f"Invalid configuration: {e}")
    if not odoo_client.connect():
        sys.exit("Failed to connect to Odoo. Please check ODOO_URL and ODOO_API_KEY.")
    
    # Get recent shipments from Odoo
    print("Fetching recent shipments from Odoo...")
    shipments = odoo_client.get_recent_shipments()
    
    if not shipments:
        print("No shipments found with DHL tracking numbers.")
        return
    
    print(f"Found {len(shipments)} shipments with DHL tracking numbers.")
    
    # Track each shipment and process the data
    results = []
    
    for shipment in shipments:
        print(f"Tracking shipment {shipment['tracking_number']} for {shipment['partner_name']}...")
        tracking_data = dhl_tracker.track_reference(shipment['tracking_number'])
        if dhl_tracker.rate_limited:
            print("DHL rate limit reached - remaining shipments not tracked; their Odoo status is unchanged.")
            break
        
        # Update Odoo (and alert on a new action code)
        shipment_sync.apply(shipment, tracking_data)
        
        processed_data = process_shipment_data(shipment, tracking_data)
        results.append(processed_data)
    
    # Save the results to a JSON file
    output_file = f"dhl_tracking_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(output_file, 'w') as f:
        json.dump(results, f, indent=2)
    
    print(f"Tracking results saved to {output_file}")

if __name__ == "__main__":
    main()