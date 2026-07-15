# Production evaluation corpus

`gold_sales_orders_v1.json` is a versioned, synthetic ten-case benchmark manifest. It covers digital and scanned inputs, three currencies, locale-sensitive dates, multiple pages and lines, missing optional values, ambiguous catalog identifiers, duplicate purchase orders, large quantities, and a required-field negative case.

The manifest contains no customer data and no document bytes. Before a production promotion, import equivalent synthetic source files whose SHA-256 values match the manifest, publish the dataset version, execute both the current and candidate configurations, and retain the resulting evaluation run IDs. Customer-derived examples belong in a tenant-confidential dataset, never in this repository.
