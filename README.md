# Cyan-DHL-Tracker

A command-line interface tool to track DHL shipments with data from Odoo.

## Features

- Fetch shipment tracking data from DHL API
- Integrate with Odoo to get partner information and shipment history
- Track individual shipments by tracking number
- View recent shipments from Odoo
- Look up partner information
- Detailed tracking history with status updates

## Installation

1. Clone this repository:
   ```
   git clone https://github.com/AlanOgic/Cyan-DHL-Tracker.git
   cd Cyan-DHL-Tracker
   ```

2. Create a virtual environment and install dependencies:
   ```
   python -m venv .venvtrack
   source .venvtrack/bin/activate  # On Windows: .venvtrack\Scripts\activate
   pip install -r requirements.txt
   ```

3. Configure your environment:
   ```
   cp .env.example .env
   ```
   
4. Edit the `.env` file with your DHL API key and Odoo credentials:
   ```
   # DHL API credentials
   DHL_API_KEY=your_dhl_api_key

   # Odoo connection details
   ODOO_URL=https://your-odoo-instance.odoo.com
   ODOO_DB=your_odoo_database
   ODOO_USERNAME=your_odoo_username
   ODOO_PASSWORD=your_odoo_password
   ```

## Usage

Run the ShipTracker CLI:

```
python shiptracker.py
```

### Main Menu Options

1. **Track a shipment** - Enter a tracking number to get detailed status
2. **View recent shipments** - List recent shipments from Odoo with tracking numbers
3. **Get partner information** - Search for partner data by ID or name
4. **Exit** - Quit the application

## DHL API Documentation

For more information about the DHL Tracking API, refer to the documentation in the `dhl-doc` directory:

- `track.yaml` - OpenAPI specification for the DHL Tracking API
- `Group Shipment Tracking Request.postman_collection.json` - Postman collection for testing

## Requirements

- Python 3.6+
- Internet connection to access DHL API
- Valid DHL API key
- Access to an Odoo instance with shipping data
