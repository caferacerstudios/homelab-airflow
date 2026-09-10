FROM apache/airflow:3.3.1-python3.12

RUN pip install --no-cache-dir \
    "apache-airflow==3.3.1" \
    "apache-airflow-providers-fab==3.8.0" \
    "apache-airflow-providers-standard==1.17.0" \
    "apache-airflow-providers-postgres==7.0.1" \
    "apache-airflow-providers-http==6.0.5" \
    "apache-airflow-providers-ssh==6.0.1" \
    "apache-airflow-providers-sftp==6.0.1" \
    "psycopg2-binary==2.9.12" \
    --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-3.3.1/constraints-3.12.txt"

RUN pip check
