from airflow.hooks.mssql_hook import MsSqlHook
import logging

import pymssql


class AstroMsSqlHook(MsSqlHook):
    def get_conn(self):
        """
        Overwrites get_conn to return rows as dictionaries.
        """
        conn = self.get_connection(self.mssql_conn_id)
        conn = pymssql.connect(
            server=conn.host,
            user=conn.login,
            password=conn.password,
            database=conn.schema,
            port=conn.port,
            as_dict=True)
        return conn

    def get_schema(self, table):
        query = \
            """
                SELECT COLUMN_NAME, DATA_TYPE
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = '{0}';
            """.format(table)
        logging.info("SCHEMA QUERY:")
        logging.info(query)
        self.schema = 'information_schema'
        return super().get_records(query)
