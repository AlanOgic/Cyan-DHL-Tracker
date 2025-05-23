#!/usr/bin/env python3
import os
import json
import requests
import xmlrpc.client
from dotenv import load_dotenv
from datetime import datetime

# Load environment variables
load_dotenv()

class OdooClient:
    def __init__(self):
        self.url = os.getenv('ODOO_URL')
        self.db = os.getenv('ODOO_DB')
        self.username = os.getenv('ODOO_USERNAME')
        self.password = os.getenv('ODOO_PASSWORD')
        self.common = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/common')
        self.uid = self.common.authenticate(self.db, self.username, self.password, {})
        self.models = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/object')
    
    def get_recent_shipments(self, limit=20):
        """
        Fetches recent shipments with tracking numbers from Odoo.
        
        Returns a list of dictionaries containing:
        - tracking_number: The DHL tracking number
        - partner_id: The ID of the partner (customer)
        - partner_name: The name of the partner
        """
        # Fetch recent shipments with tracking numbers
        # Adapt the model and fields to match your Odoo structure
        shipments = self.models.execute_kw(
            self.db, self.uid, self.password,
            'stock.picking', 'search_read',
            [
                [
                    ('carrier_tracking_ref', '!=', False),
                    ('carrier_id.name', 'ilike', 'DHL'),
                    ('state', '=', 'done')
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
        
        if response.status_code == 200:
            return response.json()
        else:
            return {
                "error": True,
                "status_code": response.status_code,
                "message": response.text
            }

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
    # Initialize clients
    odoo_client = OdooClient()
    dhl_tracker = DHLTracker()
    
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
        tracking_data = dhl_tracker.track_shipment(shipment['tracking_number'])
        processed_data = process_shipment_data(shipment, tracking_data)
        results.append(processed_data)
    
    # Save the results to a JSON file
    output_file = f"dhl_tracking_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(output_file, 'w') as f:
        json.dump(results, f, indent=2)
    
    print(f"Tracking results saved to {output_file}")

if __name__ == "__main__":
    main()