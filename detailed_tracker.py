#!/usr/bin/env python3
import os
import json
import requests
import time
from dotenv import load_dotenv
from datetime import datetime

# Load environment variables
load_dotenv()

class DHLTracker:
    def __init__(self):
        self.api_key = os.getenv('DHL_API_KEY')
        self.base_url = "https://api-eu.dhl.com/track/shipments"
    
    def track_shipment(self, tracking_number, service=None):
        """
        Tracks a DHL shipment using the DHL Tracking API.
        
        Args:
            tracking_number: The DHL tracking number
            service: Optional DHL service to use (e.g., express, parcel-de)
            
        Returns:
            Dictionary containing the tracking information
        """
        headers = {
            "DHL-API-Key": self.api_key,
            "Accept": "application/json"
        }
        
        params = {
            "trackingNumber": tracking_number,
        }
        
        if service:
            params["service"] = service
        
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

def extract_detailed_status(tracking_data):
    """
    Extracts detailed status information from DHL tracking data.
    
    Args:
        tracking_data: Dictionary containing DHL tracking data
        
    Returns:
        Dictionary containing detailed status information
    """
    result = {
        "tracking_number": None,
        "service": None,
        "current_status": None,
        "current_status_timestamp": None,
        "estimated_delivery": None,
        "next_steps": None,
        "detailed_events": [],
        "origin": None,
        "destination": None,
        "product_details": None,
        "references": [],
        "raw_response": tracking_data
    }
    
    # Check if there was an error with the tracking
    if tracking_data.get("error"):
        result["error"] = tracking_data.get("message", "Unknown error")
        result["status_code"] = tracking_data.get("status_code")
        return result
    
    # Process shipment data from DHL response
    if "shipments" in tracking_data and tracking_data["shipments"]:
        shipment = tracking_data["shipments"][0]
        
        # Basic shipment information
        result["tracking_number"] = shipment.get("id")
        result["service"] = shipment.get("service")
        
        # Current status
        if "status" in shipment:
            status = shipment["status"]
            result["current_status"] = status.get("status")
            result["current_status_code"] = status.get("statusCode")
            result["current_status_timestamp"] = status.get("timestamp")
            result["current_status_description"] = status.get("description")
            result["next_steps"] = status.get("nextSteps")
            
            if "location" in status and "address" in status["location"]:
                result["current_status_location"] = status["location"]["address"]
        
        # Estimated delivery
        result["estimated_delivery"] = shipment.get("estimatedTimeOfDelivery")
        
        if "estimatedDeliveryTimeFrame" in shipment:
            result["estimated_delivery_from"] = shipment["estimatedDeliveryTimeFrame"].get("estimatedFrom")
            result["estimated_delivery_to"] = shipment["estimatedDeliveryTimeFrame"].get("estimatedThrough")
        
        # Origin and destination
        if "origin" in shipment and "address" in shipment["origin"]:
            result["origin"] = shipment["origin"]["address"]
        
        if "destination" in shipment and "address" in shipment["destination"]:
            result["destination"] = shipment["destination"]["address"]
        
        # Product details
        if "details" in shipment and "product" in shipment["details"]:
            result["product_details"] = shipment["details"]["product"]
        
        # References
        if "details" in shipment and "references" in shipment["details"]:
            result["references"] = shipment["details"]["references"]
        
        # Detailed events
        if "events" in shipment:
            for event in shipment["events"]:
                event_data = {
                    "timestamp": event.get("timestamp"),
                    "status": event.get("status"),
                    "status_code": event.get("statusCode"),
                    "description": event.get("description"),
                }
                
                if "location" in event and "address" in event["location"]:
                    event_data["location"] = event["location"]["address"]
                
                result["detailed_events"].append(event_data)
    
    return result

def main():
    # Initialize tracker
    dhl_tracker = DHLTracker()
    
    # Get tracking number from user
    tracking_number = input("Enter DHL tracking number: ")
    
    # Ask if the user wants to specify a service
    use_service = input("Do you want to specify a DHL service? (y/n): ").lower() == 'y'
    service = None
    
    if use_service:
        print("Available services: express, parcel-de, ecommerce, dgf, parcel-uk, post-de, sameday, freight, parcel-nl, parcel-pl, dsc")
        service = input("Enter DHL service: ")
    
    # Track the shipment
    print(f"Tracking shipment {tracking_number}...")
    tracking_data = dhl_tracker.track_shipment(tracking_number, service)
    
    # Extract detailed status
    detailed_status = extract_detailed_status(tracking_data)
    
    # Save the results to a JSON file
    output_file = f"dhl_tracking_{tracking_number}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(output_file, 'w') as f:
        json.dump(detailed_status, f, indent=2)
    
    print(f"Detailed tracking results saved to {output_file}")
    
    # Print some basic information
    if detailed_status.get("error"):
        print(f"Error: {detailed_status['error']}")
    else:
        print("\nTracking Summary:")
        print(f"Tracking Number: {detailed_status['tracking_number']}")
        print(f"Service: {detailed_status['service']}")
        print(f"Current Status: {detailed_status['current_status']} ({detailed_status['current_status_code']})")
        print(f"Status Timestamp: {detailed_status['current_status_timestamp']}")
        
        if detailed_status['estimated_delivery']:
            print(f"Estimated Delivery: {detailed_status['estimated_delivery']}")
        
        if detailed_status['next_steps']:
            print(f"\nNext Steps: {detailed_status['next_steps']}")
        
        if detailed_status['detailed_events']:
            print("\nRecent Events:")
            for i, event in enumerate(detailed_status['detailed_events'][:3]):
                print(f"  {event['timestamp']} - {event['status']}")

if __name__ == "__main__":
    main()