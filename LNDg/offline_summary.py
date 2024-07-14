import os
import psycopg2
import pandas as pd
from datetime import datetime
from prettytable import PrettyTable
import configparser

# Get the path to the parent directory
parent_dir = os.path.dirname(os.path.abspath(__file__))

# Construct the path to the config.ini file
config_file_path = os.path.join(parent_dir, '..', 'config.ini')
config = configparser.ConfigParser()
config.read(config_file_path)

# Database connection parameters (replace with your actual credentials)
db_params = {
    'database': config['database']['name'],
    'user': config['database']['user'],
    'password': config['database']['password'],
    'host': config['database']['host'],
    'port': config['database']['port']
}

# Initialize connection and cursor outside of the try block
connection = None
cursor = None

# Execute the query
def calculate_offline_durations_postgresql(db_params):
    cursor = None
    try:
        # Connect to the database (assign to the global connection variable)
        global connection
        connection = psycopg2.connect(**db_params)
        cursor = connection.cursor()

        # Define and execute the SQL query
        query = """
        WITH offline_events AS (
            SELECT
                *,
                LEAD(timestamp) OVER (PARTITION BY peer_alias ORDER BY timestamp) AS online_timestamp,
                LEAD(new_value) OVER (PARTITION BY peer_alias ORDER BY timestamp) AS next_new_value,
                SUM(CASE WHEN new_value = 1 THEN 1 ELSE 0 END) OVER (PARTITION BY peer_alias ORDER BY timestamp) AS offline_session_id
            FROM gui_peerevents
            WHERE event = 'Connection' 
                AND (
                    timestamp >= '2024-07-01' AND timestamp < '2024-08-01'
                )
        ),
        offline_durations AS (
            SELECT
                peer_alias,
                offline_session_id,
                SUM(
                    CASE
                        WHEN next_new_value != 0 THEN COALESCE(online_timestamp, 'infinity'::timestamp) - timestamp
                        ELSE interval '0 seconds'
                    END
                ) AS total_offline_duration
            FROM offline_events
            WHERE new_value = 0 -- Only keep end of offline events (now 0)
            GROUP BY 1, 2
        ), 
        aggregated_durations AS (
            SELECT
                AVG(total_offline_duration) AS avg_offline_duration,
                SUM(total_offline_duration) AS total_offline_duration
            FROM offline_durations
        )
        SELECT * FROM aggregated_durations;
        """
        cursor.execute(query)

        # Fetch all rows
        results = cursor.fetchall()

        # Create a DataFrame and convert durations to hours
        df = pd.DataFrame(results, columns=["avg_offline_duration", "total_offline_duration"])
        df['avg_offline_duration'] = df['avg_offline_duration'].apply(lambda x: x.total_seconds() / 3600)
        df['total_offline_duration'] = df['total_offline_duration'].apply(lambda x: x.total_seconds() / 3600)

        return df

    except (Exception, psycopg2.Error) as error:
        print("Error while connecting to PostgreSQL:", error)
    finally:
        # Closing database connection
        if cursor:
            cursor.close()
        if connection:
            connection.close()


# Create a PrettyTable
def print_results(offline_durations):
    table = PrettyTable()
    table.field_names = ["Avg Offline (hours)", "Total Offline (hours)"]

    # Add rows to the table
    for index, row in offline_durations.iterrows():
        table.add_row([f'{row["avg_offline_duration"]:.2f}', f'{row["total_offline_duration"]:.2f}'])

    print(table)


if __name__ == '__main__':

    offline_durations = calculate_offline_durations_postgresql(db_params)
    print_results(offline_durations)
