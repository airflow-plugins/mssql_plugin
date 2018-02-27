import json
import os
import smart_open
import logging

from airflow.models import BaseOperator
from airflow.hooks.S3_hook import S3Hook
from airflow.hooks.base_hook import BaseHook
from airflow.utils.decorators import apply_defaults

from mssql_plugin.hooks.astro_mssql_hook import AstroMsSqlHook


class MsSQLToS3Operator(BaseOperator):
    """
    MsSQL to Spreadsheet Operator

    NOTE: Because this operator accesses a single database via concurrent
    connections, it is advised that a connection pool be used to control
    requests. - https://airflow.incubator.apache.org/concepts.html#pools

    :param mssql_conn_id:           The input mssql connection id.
    :type mssql_conn_id:            string
    :param mssql_table:             The input MsSQL table to pull data from.
    :type mssql_table:              string
    :param s3_conn_id:              The destination s3 connection id.
    :type s3_conn_id:               string
    :param s3_bucket:               The destination s3 bucket.
    :type s3_bucket:                string
    :param s3_key:                  The destination s3 key.
    :type s3_key:                   string
    :param batchsize                *(optional)* The number of rows you want to
                                    batch inserts with. For files that are too
                                    large for the docker container.
    :type batch:                    string
    :param primary_key:             The key used for batch streaming. Will use
                                    this to chunk your results. Assumes INT.
    :type primary_key:              string
    :param package_schema:          *(optional)* Whether or not to pull the
                                    schema information for the table as well as
                                    the data.
    :type package_schema:           boolean
    :param incremental_key:         *(optional)* The incrementing key to filter
                                    the source data with. Currently only
                                    accepts a column with type of timestamp.
    :type incremental_key:          string
    :param start:                   *(optional)* The start date to filter
                                    records with based on the incremental_key.
                                    Only required if using the incremental_key
                                    field.
    :type start:                    timestamp (YYYY-MM-DD HH:MM:SS)
    :param end:                     *(optional)* The end date to filter
                                    records with based on the incremental_key.
                                    Only required if using the incremental_key
                                    field.
    :type end:                      timestamp (YYYY-MM-DD HH:MM:SS)
    """

    template_fields = ['start', 'end', 's3_key']

    @apply_defaults
    def __init__(self,
                 mssql_conn_id,
                 mssql_table,
                 s3_conn_id,
                 s3_bucket,
                 s3_key,
                 primary_key,
                 batchsize=False,
                 package_schema=False,
                 incremental_key=None,
                 start=None,
                 end=None,
                 *args,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.mssql_conn_id = mssql_conn_id
        self.mssql_table = mssql_table
        self.s3_conn_id = s3_conn_id
        self.s3_bucket = s3_bucket
        self.s3_key = s3_key
        self.batchsize = batchsize
        self.primary_key = primary_key
        self.package_schema = package_schema
        self.incremental_key = incremental_key
        self.start = start
        self.end = end

    def execute(self, context):
        hook = AstroMsSqlHook(self.mssql_conn_id)
        self.build_fetch_query(hook)
        if self.package_schema:
            self.get_schema(hook, self.mssql_table)

    def get_schema(self, hook, table):
        logging.info('Initiating schema retrieval.')
        results = list(hook.get_schema(table))
        logging.info("Schema:")
        output_dict = {}
        for i in results:
            new = []
            new_dict = {}
            for n in i:
                if n == 'COLUMN_NAME':
                    new.insert(0, i[n])
                else:
                    new.insert(1, i[n])
            # Convert all column names to lower() for easy copy to Redshift.
            new = [i.lower() for i in new]
            if len(new) == 2:
                new_dict[new[0]] = new[1]
                output_dict.update(new_dict)

        output_dict = self.map_datatypes(output_dict)
        logging.info('Mapped Schema:')
        logging.info(output_dict)

        self.s3_upload(str(output_dict), schema=True)

    def map_datatypes(self, schema):
        # Assumes going into Redshift.
        # Maps here to making sinks easier.

        maps = {'smallint': 'INTEGER',
                'varchar': 'VARCHAR',
                'text': 'VARCHAR',
                'int': 'INTEGER',
                'float': 'FLOAT',
                'money': 'FLOAT',
                'datetime': 'TIMESTAMP',
                'bit': 'BOOLEAN',
                'char': 'VARCHAR',
                'tinyint': 'INTEGER',
                'smalldatetime': 'TIMESTAMP',
                'real': 'FLOAT'
                }
        return {v: maps[schema[v]] for v in schema}

    def build_fetch_query(self, hook):
        # Builds the part of the fetch query with the incremental_key

        logging.info('Initiating record retrieval.')
        logging.info('Start Date: {0}'.format(self.start))
        logging.info('End Date: {0}'.format(self.end))

        if all([self.incremental_key, self.start, self.end]):
            query_filter = """ WHERE {0} >= '{1}' AND {0} < '{2}'
                """.format(self.incremental_key, self.start, self.end)

        if all([self.incremental_key, self.start]) and not self.end:
            query_filter = """ WHERE {0} >= '{1}'
                """.format(self.incremental_key, self.start)

        if not self.incremental_key:
            query_filter = ''

        if self.batchsize:
            self.get_records_batch(hook, query_filter)
        else:
            self.get_records_all(hook, query_filter)

    def get_records_all(self, hook, query_filter):
        query = \
            """
            SELECT *
            FROM {0}
            {1}
            """.format(self.mssql_table, query_filter)

        # Perform query and convert returned tuple to list
        results = list(hook.get_records(query))
        logging.info('Successfully performed query.')
        logging.info('QUERY:')
        results = [dict([k.lower(), str(v)] if v is not None else [k, v]
                        for k, v in i.items()) for i in results]
        results = '\n'.join([json.dumps(i) for i in results])
        self.s3_upload(results)
        return results

    def get_records_batch(self, hook, query_filter):
        # Chunks the records and streams to s3 by specified batchsize.

        if query_filter == '':
            query_filter = 'WHERE'
        else:
            query_filter = query_filter + ' AND '

        count_sql_max = """
        SELECT max({0}) as c FROM {1} """.format(
            self.primary_key,
            self.mssql_table)

        count_sql_min = """
        SELECT min({0}) as c FROM {1} """.format(
            self.primary_key,
            self.mssql_table)

        if query_filter != 'WHERE':
            # Remove the AND from the query filter so you're only batching
            # for incremental loads within your timerange. Assumes primary_key
            # is incremental.
            count_sql_max += query_filter.split("AND")[0]
            count_sql_min += query_filter.split("AND")[0]

        count = hook.get_pandas_df(count_sql_max)['c'][0]
        min_count = hook.get_pandas_df(count_sql_min)['c'][0]

        s3_conn = BaseHook('S3').get_connection(self.s3_conn_id)
        s3_creds = s3_conn.extra_dejson

        s3_key = '{}/{}'.format(
            self.s3_bucket,
            self.s3_key
        )

        url = 's3://{}:{}@{}'.format(
            s3_creds['aws_access_key_id'],
            s3_creds['aws_secret_access_key'],
            s3_key
        )

        logging.info('Initiating record retrieval in batches.')
        logging.info('Start Date: {0}'.format(self.start))
        logging.info('End Date: {0}'.format(self.end))
        logging.info('smallest_number: {0}'.format(min_count))
        logging.info('count: {0}'.format(count))

        # Smart Open is a library for efficiently streaming large files to S3.
        # Streaming data to S3 here so it doesn't break the task container.
        # https://pypi.python.org/pypi/smart_open
        # Does this here because smart_open doesn't yet support an
        # append mode and doing it as a function was causing the file to be
        # overwritten every time.

        with smart_open.smart_open(url, 'wb') as fout:
            logging.info("First Row {0}".format(min_count)),
            logging.info("Total Rows: {0}".format(count))
            logging.info("Batch Size: {0}".format(self.batchsize))
            for batch in range(min_count, count, self.batchsize):
                query = \
                    """
                    SELECT  *
                    FROM {table}
                    {query_filter} {primary_key} >= {batch}
                    AND {primary_key} < {batch_two};
                    """.format(count=count,
                               table=self.mssql_table,
                               primary_key=self.primary_key,
                               query_filter=query_filter,
                               batch=batch,
                               batch_two=batch + self.batchsize)

                logging.info(query)

                # Perform query and convert returned tuple to list
                results = list(hook.get_records(query))
                logging.info('Successfully performed query for batch {0}-{1}.'
                             .format(batch, (batch + self.batchsize)))

                results = [dict([k.lower(), str(v)] if v is not
                                None else [k, v]
                                for k, v in i.items()) for i in results]
                results = '\n'.join([json.dumps(i) for i in results])
                # Write the results to bytes.
                results = results.encode('utf-8')
                logging.info("Uploading!")
                fout.write(results)

    def s3_upload(self, results, schema=False):
        s3 = S3Hook(s3_conn_id=self.s3_conn_id)
        key = '{0}'.format(self.s3_key)

        file_name = os.path.splitext(key)[0]
        file_extension = os.path.splitext(key)[1]
        # If the file being uploaded to s3 is a schema, append "_schema" to the
        # end of the file name.
        if schema and file_extension == '.json':
            key = file_name + '_schema' + file_extension
        if schema and file_extension == '.csv':
            key = file_name + '_schema' + file_extension
        s3.load_string(
            string_data=results,
            bucket_name=self.s3_bucket,
            key=key,
            replace=True
        )

        s3.connection.close()
        logging.info('File uploaded to s3')
