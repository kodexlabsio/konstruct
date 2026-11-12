-- The tpch_rev_analysis example with divergences planted.
CREATE OR REPLACE SEMANTIC VIEW tpch_variant
  TABLES (
    orders AS SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS PRIMARY KEY (o_orderkey),
    customers AS SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER PRIMARY KEY (c_custkey)
  )
  RELATIONSHIPS (
    -- planted: the code's relationship refers from orders to customers, this the other way round
    customers_to_orders AS customers (c_custkey) REFERENCES orders (o_custkey)
  )
  FACTS (
    -- planted: the key of the orders table, declared a fact
    orders.order_key AS o_orderkey
  )
  DIMENSIONS (
    -- planted: the code renamed o_orderdate to o_order_date
    orders.order_date AS o_orderdate,
    customers.customer_name AS c_name
  )
  METRICS (
    orders.order_total AS SUM(orders.o_totalprice)
  );
