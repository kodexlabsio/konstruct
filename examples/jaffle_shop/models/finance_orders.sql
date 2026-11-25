-- Orders as finance reports them: the payments on each order that was not returned.

with orders as (

    select * from {{ ref('stg_orders') }}

),

payments as (

    select * from {{ ref('stg_payments') }}

),

final as (

    select
        orders.order_id,
        orders.customer_id,
        orders.order_date,
        sum(payments.amount) as order_value

    from orders

    join payments
        on payments.order_id = orders.order_id

    where orders.status not in ('returned', 'return_pending')

    group by orders.order_id, orders.customer_id, orders.order_date

)

select * from final
