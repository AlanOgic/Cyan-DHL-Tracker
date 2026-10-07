#!/usr/bin/env python3
import os
import logging
import requests
import time
import schedule
from datetime import datetime, timedelta
from dotenv import load_dotenv

from alert_dispatch import AlertConfigError
from dhl_client import DHLConfigError, DHLTracker
from odoo_json2 import OdooConfigError
from shipment_sync import build_from_env

# Load environment variables
load_dotenv()

TRACKING_WINDOW_DAYS = 90
MAX_TRACKED_SHIPMENTS = 100
MAX_DELIVERED_PRELOAD = 1000

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
        message += f"• Newly delivered: {summary.get('newly_delivered', 0)}\n"
        if summary.get('postponed'):
            message += f"• Postponed (DHL rate limit): {summary['postponed']}\n"
        message += "\n"
        
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
    def __init__(self, odoo_client=None, dhl_tracker=None, webhook_sender=None, shipment_sync=None):
        """Clients default to the environment's settings; pass them to replace (e.g. in tests)."""
        self.odoo_client, self.shipment_sync = (
            (odoo_client, shipment_sync) if shipment_sync is not None else build_from_env()
        )
        self.dhl_tracker = dhl_tracker or DHLTracker()
        self.webhook_sender = webhook_sender or WebhookSender()
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

        delivered_refs = self.odoo_client.get_delivered_tracking_refs(
            limit=MAX_DELIVERED_PRELOAD, since=self._tracking_window_start()
        )
        self.last_delivered_shipments = self.last_delivered_shipments | delivered_refs
        print(f"[{datetime.now()}] Loaded {len(delivered_refs)} already delivered shipments")

    def _tracking_window_start(self):
        """Only shipments done within the tracking window are followed."""
        return datetime.now() - timedelta(days=TRACKING_WINDOW_DAYS)

    def _fetch_tracked_shipments(self):
        return self.odoo_client.get_recent_shipments(
            limit=MAX_TRACKED_SHIPMENTS, since=self._tracking_window_start()
        )

    def simple_check(self):
        """
        Simple 10-minute check - only sends notification if no changes
        """
        print(f"\n[{datetime.now()}] Simple check (10-min)...")
        
        if not self.odoo_client.connect():
            print(f"[{datetime.now()}] Failed to connect to Odoo, skipping simple check")
            return
        
        # Get shipments count only
        shipments = self._fetch_tracked_shipments()
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
        
        # Undelivered pickings past the window can no longer be tracked reliably
        expired = self.odoo_client.expire_stale_tracking(older_than=self._tracking_window_start())
        if expired:
            print(f"[{datetime.now()}] Tracking expired for {expired} DHL shipment(s) older than {TRACKING_WINDOW_DAYS} days")
        
        # Get shipments from Odoo
        shipments = self._fetch_tracked_shipments()
        
        if not shipments:
            print(f"[{datetime.now()}] No shipments found")
            return
        
        print(f"[{datetime.now()}] Processing {len(shipments)} shipments for hourly report...")
        
        in_transit_shipments = []
        newly_delivered_shipments = []
        shipments_with_next_steps = []
        postponed = 0
        
        for index, shipment in enumerate(shipments):
            tracking_number = shipment['tracking_number']
            
            # Check if already delivered in our tracking system
            if tracking_number in self.last_delivered_shipments:
                print(f"[{datetime.now()}] {tracking_number} - SKIPPED: Already delivered")
                continue
            
            # Get status from DHL
            print(f"[{datetime.now()}] Tracking {tracking_number}...")
            tracking_data = self.dhl_tracker.track_reference(tracking_number)
            
            # DHL keeps refusing calls: keep the last known Odoo status and retry at the next check
            if self.dhl_tracker.rate_limited:
                postponed = len(shipments) - index
                print(f"[{datetime.now()}] DHL rate limit reached - {postponed} shipment(s) postponed to the next check")
                break
            
            # Update Odoo (and alert on a new action code)
            result = self.shipment_sync.apply(shipment, tracking_data)
            status_description, next_steps = result.status, result.next_steps
            if result.alerted:
                print(f"[{datetime.now()}] {tracking_number} - ALERT [{result.event_code}] raised for {shipment['partner_name']}")
            
            shipment_data = {
                'tracking_number': tracking_number,
                'partner_name': shipment['partner_name'],
                'partner_id': shipment['partner_id'],
                'shipment_ref': shipment['shipment_ref'],
                'status': status_description,
                'next_steps': next_steps,
                'is_delivered': result.delivered,
                'timestamp': datetime.now().isoformat()
            }
            
            if result.delivered:
                # Add to newly delivered list
                newly_delivered_shipments.append(shipment_data)
                self.last_delivered_shipments.add(tracking_number)
                print(f"[{datetime.now()}] {tracking_number} - NEWLY DELIVERED for {shipment['partner_name']}")
                
            else:
                in_transit_shipments.append(shipment_data)
                
                # Check if it has next steps for detailed report
                if next_steps:
                    shipments_with_next_steps.append(shipment_data)
                
                print(f"[{datetime.now()}] {tracking_number} - IN TRANSIT: {status_description}")
        
        # Send hourly detailed webhook
        webhook_data = {
            'timestamp': datetime.now().isoformat(),
            'summary': {
                'total_shipments': len(shipments),
                'in_transit': len(in_transit_shipments),
                'newly_delivered': len(newly_delivered_shipments),
                'postponed': postponed
            },
            'in_transit_shipments': in_transit_shipments,
            'newly_delivered_shipments': newly_delivered_shipments
        }
        
        self.webhook_sender.send_webhook(webhook_data)
        
        # Send detailed report for shipments with next steps
        if shipments_with_next_steps:
            print(f"[{datetime.now()}] Sending detailed report for {len(shipments_with_next_steps)} shipments with next steps")
            self.send_detailed_next_steps_report(shipments_with_next_steps)
        
        print(f"[{datetime.now()}] Hourly check completed - {len(in_transit_shipments)} in transit, {len(newly_delivered_shipments)} newly delivered, {postponed} postponed")
    
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
            shipments = self._fetch_tracked_shipments()
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
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")

    try:
        tracker = AutomatedTracker()
    except (OdooConfigError, DHLConfigError, AlertConfigError) as e:
        raise SystemExit(f"[{datetime.now()}] Invalid configuration: {e}")

    try:
        tracker.start_scheduler()
    except KeyboardInterrupt:
        print(f"\n[{datetime.now()}] Automated tracker stopped by user")
    except Exception as e:
        print(f"\n[{datetime.now()}] Error in automated tracker: {str(e)}")

if __name__ == "__main__":
    main()