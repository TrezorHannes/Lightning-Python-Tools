import psycopg2
import pandas as pd
from datetime import datetime
from prettytable import PrettyTable
import argparse
import configparser
import os

# Get the path to the parent directory
parent_dir = os.path.dirname(os.path.abspath(__file__))

# Construct the path to the config.ini file
config_file_path = os.path.join(parent_dir, '..', 'config.ini')
config = configparser.ConfigParser()
config.read(config_file_path)

# Database connection parameters (replace with your actual credentials)
db_params = {
    'database': config['database']['name'],
    'user': 'admin',  # Using 'admin' user for peer authentication
    'host': config['database']['host'],
    'port': config['database']['port']
}


def calculate_offline_durations(start_date, sort_by='total_offline_duration', sort_order='DESC'):
    try:
        # Connect to the database (with peer auth set in .postgres/pg_hba.conf)
        connection = psycopg2.connect(**db_params)
        cursor = connection.cursor()

        # Define and execute the SQL query
        query = f"""
        WITH offline_events AS (
            SELECT 
                *,
                LEAD(timestamp) OVER (PARTITION BY peer_alias ORDER BY timestamp) AS online_timestamp,
                SUM(CASE WHEN new_value = 1 THEN 1 ELSE 0 END) OVER (PARTITION BY peer_alias ORDER BY timestamp) AS offline_session_id
            FROM gui_peerevents
            WHERE event = 'Connection' 
        ),
        offline_durations AS (
            SELECT
                peer_alias,
                offline_session_id,
                MAX(COALESCE(online_timestamp, timestamp + interval '1 hour')) AS offline_end,
                MIN(timestamp) AS offline_start
            FROM offline_events
            WHERE new_value = 0 AND DATE_TRUNC('month', timestamp) = '{start_date}'
            GROUP BY 1, 2
        )
        SELECT
            peer_alias,
            ROUND(EXTRACT(epoch FROM AVG(offline_end - offline_start)) / 3600, 2) AS avg_offline_hours,
            ROUND(EXTRACT(epoch FROM SUM(offline_end - offline_start)) / 3600, 2) AS total_offline_hours
        FROM offline_durations
        GROUP BY 1
        ORDER BY {sort_by} {sort_order}
        """
        cursor.execute(query)
        results = cursor.fetchall()

        # Create a DataFrame
        df = pd.DataFrame(results, columns=["peer_alias", "avg_offline_hours", "total_offline_hours"])
        return df
    except (Exception, psycopg2.Error) as error:
        print("Error while connecting to PostgreSQL:", error)
    finally:
        # Closing database connection
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'connection' in locals() and connection:
            connection.close()
def print_results(offline_durations):
    table = PrettyTable()
    table.field_names = ["Peer Alias", "Avg Offline (hours)", "Total Offline (hours)"]
    for _, row in offline_durations.iterrows():
        table.add_row(row)
    print(table)
    

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Calculate offline durations for LND peers.")
    parser.add_argument("--month", type=str, required=True, help="Month to analyze (YYYY-MM format)")
    parser.add_argument("--sort_by", type=str, choices=["peer_alias", "avg_offline_hours", "total_offline_hours"], default="total_offline_hours", help="Column to sort by")
    parser.add_argument("--sort_order", type=str, choices=["ASC", "DESC"], default="DESC", help="Sorting order (ASC or DESC)")

    args = parser.parse_args()
    offline_durations = calculate_offline_durations(args.month, args.sort_by, args.sort_order)
    print_results(offline_durations)