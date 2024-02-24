import threading

from dbt.contracts.connection import AdapterResponse
from dbt.exceptions import DbtRuntimeError

from .. import credentials, utils
from . import Environment


class DuckDBCursorWrapper:
    def __init__(self, cursor):
        self._cursor = cursor

    # forward along all non-execute() methods/attribute look-ups
    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def execute(self, sql, bindings=None):
        try:
            if bindings is None:
                return self._cursor.execute(sql)
            else:
                return self._cursor.execute(sql, bindings)
        except RuntimeError as e:
            raise DbtRuntimeError(str(e))

    
class DuckDBConnectionWrapper:
    def __init__(self, cursor, env):
        self._cursor = DuckDBCursorWrapper(cursor)
        self._env = env

    def close(self):
        self._cursor.close()
        self._env.notify_closed()

    def cursor(self):
        return self._cursor


class LocalEnvironment(Environment):
    def __init__(self, credentials: credentials.DuckDBCredentials):
        # Set the conn attribute to None so it always exists even if
        # DB initialization fails
        super().__init__(credentials)
        self.conn = None
        self._plugins = self.initialize_plugins(credentials)
        self.handle_count = 0
        self.lock = threading.RLock()
        self._keep_open = (
            credentials.keep_open or credentials.path == ":memory:" or credentials.is_motherduck
        )
        self._REGISTERED_DF: dict = {}

    def notify_closed(self):
        with self.lock:
            self.handle_count -= 1
            if self.handle_count == 0 and not self._keep_open:
                self.close()

    def handle(self):
        # Extensions/settings need to be configured per cursor
        with self.lock:
            if self.conn is None:
                self.conn = self.initialize_db(self.creds, self._plugins)
            self.handle_count += 1

        cursor = self.initialize_cursor(
            self.creds, self.conn.cursor(), self._plugins, self._REGISTERED_DF
        )
        return DuckDBConnectionWrapper(cursor, self)

    def submit_python_job(self, handle, parsed_model: dict, compiled_code: str) -> AdapterResponse:
        con = handle.cursor()

        def ldf(table_name):
            return con.query(f"select * from {table_name}")

        self.run_python_job(con, ldf, parsed_model["alias"], compiled_code, self.creds)
        return AdapterResponse(_message="OK")

    def load_source(self, plugin_name: str, source_config: utils.SourceConfig):
        if plugin_name not in self._plugins:
            raise Exception(
                f"Plugin {plugin_name} not found; known plugins are: "
                + ",".join(self._plugins.keys())
            )
        plugin = self._plugins[plugin_name]
        handle = self.handle()
        cursor = handle.cursor()

        if source_config.schema:
            cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {source_config.schema}")

        save_mode = source_config.get("save_mode", "overwrite")
        if save_mode in ("ignore", "error_if_exists"):
            params = [source_config.schema, source_config.identifier]
            q = """SELECT COUNT(1)
                FROM system.information_schema.tables
                WHERE table_schema = ?
                AND table_name = ?
                """
            if source_config.database:
                q += "AND table_catalog = ?"
                params.append(source_config.database)
            if cursor.execute(q, params).fetchone()[0]:
                if save_mode == "error_if_exists":
                    raise Exception(f"Source {source_config.table_name()} already exists!")
                else:
                    # Nothing to do (we ignore the existing table)
                    return
        df = plugin.load(source_config)
        assert df is not None

        materialization = source_config.meta.get(
            "materialization", plugin.default_materialization()
        )
        source_table_name = source_config.table_name()
        df_name = source_table_name.replace(".", "_") + "_df"

        cursor.register(df_name, df)

        if materialization == "view":
            # save to df instance to register on each cursor creation
            self._REGISTERED_DF[df_name] = df

        cursor.execute(
            f"CREATE OR REPLACE {materialization} {source_table_name} AS SELECT * FROM {df_name}"
        )

        cursor.close()
        handle.close()

    def store_relation(self, plugin_name: str, target_config: utils.TargetConfig) -> None:
        # some plugin have to be initialized on the fly? glue for example?

        if plugin_name not in self._plugins:
            raise Exception(
                f"Plugin {plugin_name} not found; known plugins are: "
                + ",".join(self._plugins.keys())
            )
        plugin = self._plugins[plugin_name]
        
        #e.g add file format to the location
        target_config = plugin.adapt_target_config(target_config)

        #export data with the store model 
        handle = self.handle()
        cursor = handle.cursor()        
        
        df = cursor.sql(target_config.config.model.compiled_code)
        #hand over Duckdb format that each plugin can choose which type of integration to use
        plugin.store(df, target_config, cursor)

        cursor.close()
        handle.close()

        # all are by default false, has to be turned on per plugin
        if plugin.can_be_upstream_referenced():
            #create df and view which can be referenced in the run following run
            source_config = plugin.create_source_config(target_config)
            self.load_source(plugin_name, source_config)

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def __del__(self):
        self.close()
