#!/usr/bin/env python3
import os
import json
import requests
import xmlrpc.client
import time
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
        # Fetch recent shipments with tracking numbers, excluding already delivered ones
        # Adapt the model and fields to match your Odoo structure
        shipments = self.models.execute_kw(
            self.db, self.uid, self.password,
            'stock.picking', 'search_read',
            [
                [
                    ('carrier_tracking_ref', '!=', False),
                    ('carrier_id.name', 'ilike', 'DHL'),
                    ('state', '=', 'done'),
                    ('x_studio_delivered_', '=', False)
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
                    [picking_ids, {'x_studio_delivered_': True, 'x_studio_last_status': ''}]
                )
            else:
                # For non-delivered: update status field with current status + next steps (multi-line)
                status_lines = []
                if current_status:
                    status_lines.append(f"Status: {current_status}")
                if next_steps:
                    status_lines.append(f"Next Steps: {next_steps}")
                status_text = "\n".join(status_lines)
                
                update_result = self.models.execute_kw(
                    self.db, self.uid, self.password,
                    'stock.picking', 'write',
                    [picking_ids, {'x_studio_last_status': status_text}]
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

def is_shipment_delivered(tracking_data):
    """
    Check if a shipment is delivered based on DHL tracking data.
    
    Args:
        tracking_data: Dictionary containing DHL tracking data
        
    Returns:
        Boolean indicating if the shipment is delivered
    """
    if tracking_data.get("error"):
        return False
    
    if "shipments" in tracking_data and tracking_data["shipments"]:
        shipment = tracking_data["shipments"][0]
        if "status" in shipment:
            status_info = shipment["status"]
            
            # Get current status description
            description = status_info.get("description", status_info.get("status", "Unknown")).lower()
            
            # Check if delivered
            status_code = status_info.get("statusCode", "").lower()
            return "delivered" in description or status_code in ["delivered", "ok"]
    
    return False

def get_status_info(tracking_data):
    """
    Extract current status and next steps from DHL tracking data.
    
    Args:
        tracking_data: Dictionary containing DHL tracking data
        
    Returns:
        Tuple of (current_status, next_steps)
    """
    if tracking_data.get("error"):
        return None, None
    
    if "shipments" in tracking_data and tracking_data["shipments"]:
        shipment = tracking_data["shipments"][0]
        if "status" in shipment:
            status_info = shipment["status"]
            current_status = status_info.get("description", status_info.get("status", "Unknown"))
            next_steps = status_info.get("nextSteps")
            return current_status, next_steps
    
    return None, None

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
        
        # Update Odoo based on delivery status
        is_delivered = is_shipment_delivered(tracking_data)
        current_status, next_steps = get_status_info(tracking_data)
        
        if is_delivered:
            odoo_client.update_delivery_status(shipment['tracking_number'], delivered=True)
        else:
            # Update status field for non-delivered shipments
            odoo_client.update_delivery_status(shipment['tracking_number'], delivered=False,
                                             current_status=current_status, next_steps=next_steps)
        
        processed_data = process_shipment_data(shipment, tracking_data)
        results.append(processed_data)
    
    # Save the results to a JSON file
    output_file = f"dhl_tracking_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(output_file, 'w') as f:
        json.dump(results, f, indent=2)
    
    print(f"Tracking results saved to {output_file}")

if __name__ == "__main__":
    main()