#!/usr/bin/env python3
import os
import json
import requests
import xmlrpc.client
import time
import ssl
import schedule
import threading
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

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
        print(f"[{datetime.now()}] Connecting to Odoo...")
        print(f"[{datetime.now()}] URL: {self.url}")
        print(f"[{datetime.now()}] DB: {self.db}")
        print(f"[{datetime.now()}] Username: {self.username}")
        
        try:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            
            print(f"[{datetime.now()}] Creating common proxy...")
            self.common = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/common', context=context)
            
            print(f"[{datetime.now()}] Authenticating...")
            self.uid = self.common.authenticate(self.db, self.username, self.password, {})
            
            if not self.uid:
                print(f"[{datetime.now()}] Authentication failed - invalid credentials")
                return False
            
            print(f"[{datetime.now()}] Creating models proxy...")
            self.models = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/object', context=context)
            
            print(f"[{datetime.now()}] Connection successful! User ID: {self.uid}")
            return True
        except Exception as e:
            print(f"[{datetime.now()}] Connection failed: {str(e)}")
            return False
    
    def get_recent_shipments(self, limit=100):
        """
        Fetches recent shipments with tracking numbers from Odoo.
        Only includes shipments from the last 3 months.
        """
        try:
            # Calculate date 3 months ago
            three_months_ago = datetime.now() - timedelta(days=90)
            date_filter = three_months_ago.strftime('%Y-%m-%d %H:%M:%S')
            
            print(f"[{datetime.now()}] Filtering shipments newer than: {date_filter}")
            
            shipments = self.models.execute_kw(
                self.db, self.uid, self.password,
                'stock.picking', 'search_read',
                [
                    [
                        ('carrier_tracking_ref', '!=', False),
                        ('carrier_id.name', 'ilike', 'DHL'),
                        ('state', '=', 'done'),
                        ('x_studio_delivered_', '=', False),
                        ('date_done', '>=', date_filter)  # Only shipments from last 3 months
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
            print(f"[{datetime.now()}] Error fetching shipments: {str(e)}")
            return []
    
    def update_delivery_status(self, tracking_number, delivered=True, current_status=None, next_steps=None):
        """
        Updates the delivery status in Odoo stock.picking model.
        """
        try:
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
                return False
            
            if delivered:
                update_result = self.models.execute_kw(
                    self.db, self.uid, self.password,
                    'stock.picking', 'write',
                    [picking_ids, {'x_studio_delivered_': True, 'x_studio_last_status': ''}]
                )
            else:
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
            
            return update_result
                
        except Exception as e:
            print(f"[{datetime.now()}] Error updating delivery status for {tracking_number}: {str(e)}")
            return False

class DHLTracker:
    def __init__(self):
        self.api_key = os.getenv('DHL_API_KEY')
        self.base_url = "https://api-eu.dhl.com/track/shipments"
    
    def track_shipment(self, tracking_number):
        """
        Tracks a DHL shipment using the DHL Tracking API.
        """
        headers = {
            "DHL-API-Key": self.api_key,
            "Accept": "application/json"
        }
        
        params = {
            "trackingNumber": tracking_number
        }
        
        try:
            response = requests.get(self.base_url, headers=headers, params=params)
            time.sleep(2)  # Rate limiting
            
            if response.status_code == 200:
                return response.json()
            else:
                return {
                    "error": True,
                    "status_code": response.status_code,
                    "message": response.text
                }
        except Exception as e:
            return {
                "error": True,
                "message": str(e)
            }
    
    def get_shipment_status(self, tracking_number):
        """
        Get current status of a shipment.
        Returns: (status_description, next_steps, is_delivered)
        """
        tracking_data = self.track_shipment(tracking_number)
        
        if tracking_data.get("error"):
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
                
                description = status_info.get("description", status_info.get("status", "Unknown"))
                status_code = status_info.get("statusCode", "").lower()
                is_delivered = "delivered" in description.lower() or status_code in ["delivered", "ok"]
                next_steps = None if is_delivered else status_info.get("nextSteps")
                
                return (description, next_steps, is_delivered)
        
        return ("No data", None, False)

class WebhookSender:
    def __init__(self):
        self.webhook_url = os.getenv('WEBHOOK_URL')
    
    def format_mattermost_message(self, data, is_startup=False):
        """
        Format data for Mattermost webhook
        """
        if is_startup:
            message = "🚀 **DHL Tracker Started**\n\n"
        else:
            message = "📦 **DHL Shipment Update**\n\n"
        
        summary = data.get('summary', {})
        
        # Summary section
        message += f"**Summary:**\n"
        message += f"• Total shipments: {summary.get('total_shipments', 0)}\n"
        message += f"• In transit: {summary.get('in_transit', 0)}\n"
        message += f"• Newly delivered: {summary.get('newly_delivered', 0)}\n\n"
        
        # Newly delivered section
        newly_delivered = data.get('newly_delivered_shipments', [])
        if newly_delivered:
            message += "✅ **Newly Delivered:**\n"
            for shipment in newly_delivered:
                message += f"• `{shipment['tracking_number']}` - {shipment['partner_name']}\n"
            message += "\n"
        
        # In transit section with full status and next steps
        in_transit = data.get('in_transit_shipments', [])
        if in_transit:
            message += "🚛 **In Transit:**\n"
            for shipment in in_transit[:10]:
                # Show full status (no truncation)
                status = shipment['status']
                message += f"• `{shipment['tracking_number']}` - {shipment['partner_name']}\n"
                message += f"  📍 Status: {status}\n"
                
                # Add next steps if available
                if shipment.get('next_steps'):
                    next_steps = shipment['next_steps']
                    message += f"  ➡️ Next Steps: {next_steps}\n"
                
                message += "\n"  # Extra line for readability
            
            if len(in_transit) > 10:
                message += f"• ... and {len(in_transit) - 10} more shipments\n\n"
        
        # Format timestamp to be more readable
        timestamp = data.get('timestamp', 'Unknown')
        if timestamp != 'Unknown':
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                formatted_time = dt.strftime('%Y-%m-%d | %H:%M:%S')
                message += f"⏰ Last updated: {formatted_time}"
            except:
                message += f"⏰ Last updated: {timestamp}"
        else:
            message += f"⏰ Last updated: {timestamp}"
        
        return {
            "text": message,
            "username": "DHL Tracker",
            "icon_emoji": ":truck:"
        }
    
    def send_webhook(self, data, is_startup=False):
        """
        Send webhook notification with shipment data formatted for Mattermost
        """
        if not self.webhook_url:
            print(f"[{datetime.now()}] No webhook URL configured")
            return False
        
        try:
            # Format for Mattermost
            mattermost_payload = self.format_mattermost_message(data, is_startup)
            
            headers = {
                'Content-Type': 'application/json',
                'User-Agent': 'DHL-Tracker-Webhook/1.0'
            }
            
            response = requests.post(self.webhook_url, json=mattermost_payload, headers=headers, timeout=30)
            
            if response.status_code == 200:
                print(f"[{datetime.now()}] Mattermost webhook sent successfully")
                return True
            else:
                print(f"[{datetime.now()}] Webhook failed with status {response.status_code}: {response.text}")
                return False
                
        except Exception as e:
            print(f"[{datetime.now()}] Error sending webhook: {str(e)}")
            return False
    
    def send_webhook_simple(self, data):
        """
        Send simple notification for 10-minute checks
        """
        if not self.webhook_url:
            return False
        
        try:
            # Format timestamp
            timestamp = data.get('timestamp', 'Unknown')
            if timestamp != 'Unknown':
                try:
                    dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    formatted_time = dt.strftime('%Y-%m-%d | %H:%M:%S')
                except:
                    formatted_time = timestamp
            else:
                formatted_time = timestamp
            
            message = f"🔄 **Simple Check**\n\n"
            message += f"📊 **Status:** {data['summary']['total_shipments']} shipments being tracked\n"
            message += f"⏰ Checked: {formatted_time}"
            
            payload = {
                "text": message,
                "username": "DHL Tracker",
                "icon_emoji": ":mag:"
            }
            
            headers = {'Content-Type': 'application/json'}
            response = requests.post(self.webhook_url, json=payload, headers=headers, timeout=30)
            
            return response.status_code == 200
            
        except Exception as e:
            print(f"[{datetime.now()}] Error sending simple webhook: {str(e)}")
            return False
    
    def send_webhook_detailed_report(self, data):
        """
        Send detailed report for shipments with next steps
        """
        if not self.webhook_url:
            return False
        
        try:
            # Format timestamp
            timestamp = data.get('timestamp', 'Unknown')
            if timestamp != 'Unknown':
                try:
                    dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    formatted_time = dt.strftime('%Y-%m-%d | %H:%M:%S')
                except:
                    formatted_time = timestamp
            else:
                formatted_time = timestamp
            
            message = f"📋 **Detailed Next Steps Report**\n\n"
            
            shipments = data.get('shipments_with_next_steps', [])
            message += f"🚨 **{len(shipments)} shipments require attention:**\n\n"
            
            for shipment in shipments:
                message += f"📦 **`{shipment['tracking_number']}`** - {shipment['partner_name']}\n"
                message += f"  📍 **Status:** {shipment['status']}\n"
                message += f"  ⚠️ **Action Required:** {shipment['next_steps']}\n"
                message += f"  🏢 **Reference:** {shipment['shipment_ref']}\n\n"
            
            message += f"⏰ Report generated: {formatted_time}"
            
            payload = {
                "text": message,
                "username": "DHL Tracker",
                "icon_emoji": ":warning:"
            }
            
            headers = {'Content-Type': 'application/json'}
            response = requests.post(self.webhook_url, json=payload, headers=headers, timeout=30)
            
            if response.status_code == 200:
                print(f"[{datetime.now()}] Detailed report webhook sent successfully")
                return True
            else:
                print(f"[{datetime.now()}] Detailed report webhook failed")
                return False
                
        except Exception as e:
            print(f"[{datetime.now()}] Error sending detailed report webhook: {str(e)}")
            return False

class AutomatedTracker:
    def __init__(self):
        self.odoo_client = OdooClient()
        self.dhl_tracker = DHLTracker()
        self.webhook_sender = WebhookSender()
        self.last_delivered_shipments = set()
        self.last_check_results = {}  # Store last check results for comparison
    
    def load_delivered_shipments(self):
        """
        Load already delivered shipments from Odoo to avoid retracking them
        """
        print(f"[{datetime.now()}] Loading already delivered shipments from Odoo...")
        
        if not self.odoo_client.connect():
            print(f"[{datetime.now()}] Failed to connect to Odoo for loading delivered shipments")
            return
        
        try:
            # Calculate date 3 months ago
            three_months_ago = datetime.now() - timedelta(days=90)
            date_filter = three_months_ago.strftime('%Y-%m-%d %H:%M:%S')
            
            # Get delivered shipments from last 3 months
            delivered_shipments = self.odoo_client.models.execute_kw(
                self.odoo_client.db, self.odoo_client.uid, self.odoo_client.password,
                'stock.picking', 'search_read',
                [
                    [
                        ('carrier_tracking_ref', '!=', False),
                        ('carrier_id.name', 'ilike', 'DHL'),
                        ('state', '=', 'done'),
                        ('x_studio_delivered_', '=', True),  # Already delivered
                        ('date_done', '>=', date_filter)
                    ]
                ],
                {
                    'fields': ['carrier_tracking_ref'],
                    'limit': 1000
                }
            )
            
            # Add to our delivered set
            for shipment in delivered_shipments:
                tracking_number = shipment['carrier_tracking_ref']
                self.last_delivered_shipments.add(tracking_number)
            
            print(f"[{datetime.now()}] Loaded {len(delivered_shipments)} already delivered shipments")
            
        except Exception as e:
            print(f"[{datetime.now()}] Error loading delivered shipments: {str(e)}")
        
    def simple_check(self):
        """
        Simple 10-minute check - only sends notification if no changes
        """
        print(f"\n[{datetime.now()}] Simple check (10-min)...")
        
        if not self.odoo_client.connect():
            print(f"[{datetime.now()}] Failed to connect to Odoo, skipping simple check")
            return
        
        # Get shipments count only
        shipments = self.odoo_client.get_recent_shipments()
        current_count = len(shipments)
        
        # Check if count changed
        last_count = self.last_check_results.get('shipment_count', 0)
        
        if current_count != last_count:
            print(f"[{datetime.now()}] Shipment count changed: {last_count} -> {current_count}")
            # Send simple update
            simple_data = {
                'timestamp': datetime.now().isoformat(),
                'summary': {
                    'total_shipments': current_count,
                    'in_transit': current_count,
                    'newly_delivered': 0
                },
                'in_transit_shipments': [],
                'newly_delivered_shipments': []
            }
            self.webhook_sender.send_webhook_simple(simple_data)
        else:
            print(f"[{datetime.now()}] No changes detected ({current_count} shipments)")
        
        # Update last check
        self.last_check_results['shipment_count'] = current_count

    def hourly_detailed_check(self):
        """
        Hourly detailed check with full tracking
        """
        print(f"\n[{datetime.now()}] Hourly detailed check...")
        
        if not self.odoo_client.connect():
            print(f"[{datetime.now()}] Failed to connect to Odoo, skipping hourly check")
            return
        
        # Get shipments from Odoo
        shipments = self.odoo_client.get_recent_shipments()
        
        if not shipments:
            print(f"[{datetime.now()}] No shipments found")
            return
        
        print(f"[{datetime.now()}] Processing {len(shipments)} shipments for hourly report...")
        
        in_transit_shipments = []
        newly_delivered_shipments = []
        shipments_with_next_steps = []
        
        for shipment in shipments:
            tracking_number = shipment['tracking_number']
            
            # Check if already delivered in our tracking system
            if tracking_number in self.last_delivered_shipments:
                print(f"[{datetime.now()}] {tracking_number} - SKIPPED: Already delivered")
                continue
            
            # Get status from DHL
            print(f"[{datetime.now()}] Tracking {tracking_number}...")
            status_description, next_steps, is_delivered = self.dhl_tracker.get_shipment_status(tracking_number)
            
            shipment_data = {
                'tracking_number': tracking_number,
                'partner_name': shipment['partner_name'],
                'partner_id': shipment['partner_id'],
                'shipment_ref': shipment['shipment_ref'],
                'status': status_description,
                'next_steps': next_steps,
                'is_delivered': is_delivered,
                'timestamp': datetime.now().isoformat()
            }
            
            # Update Odoo and handle delivery status
            if is_delivered:
                # Update Odoo to mark as delivered
                self.odoo_client.update_delivery_status(tracking_number, delivered=True)
                
                # Add to newly delivered list
                newly_delivered_shipments.append(shipment_data)
                self.last_delivered_shipments.add(tracking_number)
                print(f"[{datetime.now()}] {tracking_number} - NEWLY DELIVERED for {shipment['partner_name']}")
                
            else:
                # Update status for non-delivered shipments
                self.odoo_client.update_delivery_status(tracking_number, delivered=False,
                                                      current_status=status_description, 
                                                      next_steps=next_steps)
                in_transit_shipments.append(shipment_data)
                
                # Check if it has next steps for detailed report
                if next_steps:
                    shipments_with_next_steps.append(shipment_data)
                
                print(f"[{datetime.now()}] {tracking_number} - IN TRANSIT: {status_description}")
            
            # Rate limiting
            time.sleep(1)
        
        # Send hourly detailed webhook
        webhook_data = {
            'timestamp': datetime.now().isoformat(),
            'summary': {
                'total_shipments': len(shipments),
                'in_transit': len(in_transit_shipments),
                'newly_delivered': len(newly_delivered_shipments)
            },
            'in_transit_shipments': in_transit_shipments,
            'newly_delivered_shipments': newly_delivered_shipments
        }
        
        self.webhook_sender.send_webhook(webhook_data)
        
        # Send detailed report for shipments with next steps
        if shipments_with_next_steps:
            print(f"[{datetime.now()}] Sending detailed report for {len(shipments_with_next_steps)} shipments with next steps")
            self.send_detailed_next_steps_report(shipments_with_next_steps)
        
        print(f"[{datetime.now()}] Hourly check completed - {len(in_transit_shipments)} in transit, {len(newly_delivered_shipments)} newly delivered")
    
    def send_detailed_next_steps_report(self, shipments_with_next_steps):
        """
        Send detailed report for shipments that have next steps
        """
        detailed_data = {
            'timestamp': datetime.now().isoformat(),
            'shipments_with_next_steps': shipments_with_next_steps
        }
        
        self.webhook_sender.send_webhook_detailed_report(detailed_data)
    
    def send_startup_notification(self):
        """
        Send startup notification to Mattermost
        """
        print(f"[{datetime.now()}] Sending startup notification...")
        
        # Get initial shipment count (connection should already be established)
        try:
            shipments = self.odoo_client.get_recent_shipments()
            delivered_count = len(self.last_delivered_shipments)
            in_transit_count = len(shipments)
            
            startup_data = {
                'timestamp': datetime.now().isoformat(),
                'summary': {
                    'total_shipments': in_transit_count + delivered_count,
                    'in_transit': in_transit_count,
                    'newly_delivered': 0
                },
                'in_transit_shipments': [],
                'newly_delivered_shipments': []
            }
            
            self.webhook_sender.send_webhook(startup_data, is_startup=True)
            
        except Exception as e:
            print(f"[{datetime.now()}] Error sending startup notification: {str(e)}")
    
    def start_scheduler(self):
        """
        Start the multi-level scheduler:
        - Every 10 minutes: Simple check
        - Every hour: Detailed check with full tracking
        """
        print(f"[{datetime.now()}] Starting automated DHL tracker...")
        print(f"[{datetime.now()}] Schedule:")
        print(f"[{datetime.now()}] - Simple checks: every 10 minutes")
        print(f"[{datetime.now()}] - Detailed checks: every hour")
        print(f"[{datetime.now()}] - Next steps reports: after each hourly check")
        
        # Load already delivered shipments to avoid retracking
        self.load_delivered_shipments()
        
        # Send startup notification
        self.send_startup_notification()
        
        # Schedule different types of checks
        schedule.every(10).minutes.do(self.simple_check)
        schedule.every().hour.do(self.hourly_detailed_check)
        
        # Run initial detailed check immediately
        self.hourly_detailed_check()
        
        # Keep the scheduler running
        while True:
            schedule.run_pending()
            time.sleep(60)  # Check every minute

def main():
    print(r"""
  ____                   _____                _             
 / ___|   _  __ _ _ __   |_   _| __ __ _  ___| | _____ _ __ 
| |  | | | |/ _` | '_ \    | || '__/ _` |/ __| |/ / _ \ '__|
| |__| |_| | (_| | | | |   | || | | (_| | (__|   <  __/ |   
 \____\__, |\__,_|_| |_|   |_||_|  \__,_|\___|_|\_\___|_|   
      |___/                                                
AUTOMATED TRACKER - by Alan, for Cyanview
""")
    
    tracker = AutomatedTracker()
    
    try:
        tracker.start_scheduler()
    except KeyboardInterrupt:
        print(f"\n[{datetime.now()}] Automated tracker stopped by user")
    except Exception as e:
        print(f"\n[{datetime.now()}] Error in automated tracker: {str(e)}")

if __name__ == "__main__":
    main()