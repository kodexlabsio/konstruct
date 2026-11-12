-- Follows the statement dbt_semantic_view generates for the jaffle_shop marts.
CREATE OR REPLACE SEMANTIC VIEW jaffle_sv
  TABLES (
    customers AS jaffle_shop.main.customers PRIMARY KEY (customer_id),
    orders AS jaffle_shop.main.orders PRIMARY KEY (order_id)
  )
  RELATIONSHIPS (
    orders_to_customers AS orders (customer_id) REFERENCES customers
  )
  FACTS (
    orders.amount AS amount,
    -- planted: the key of orders, declared a fact
    orders.order_key AS order_id
  )
  DIMENSIONS (
    orders.status AS status,
    customers.first_name AS first_name
  )
  METRICS (
    orders.order_amount AS SUM(orders.amount),
    -- planted: matched to the term Completed order by its name alone
    orders.completed_order AS COUNT(orders.order_key)
  );
