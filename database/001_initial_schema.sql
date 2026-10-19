Get-ChildItem C:\Users\User\Desktop\AP\AP-Core\src\api\rest\routers -Name-- GenWorx AP Assistant initial schema based on supplied LLD.
-- Local/sample-data use only. UUID PK and standard audit columns on each business table.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE user_role AS ENUM ('associate', 'admin');
CREATE TYPE doc_type AS ENUM ('po', 'invoice', 'other');
CREATE TYPE doc_status AS ENUM ('received', 'parsing', 'parsed', 'classified', 'extracted', 'needs_vendor_review', 'processed', 'ignored', 'incomplete', 'duplicate_blocked', 'failed');CREATE TYPE match_status AS ENUM ('pending', 'needs_vendor_review', 'waiting_for_po', 'needs_po_confirmation', 'ready_for_review', 'match_issues', 'no_match');
CREATE TYPE match_result AS ENUM ('match_ok', 'match_issues', 'no_match');
CREATE TYPE review_decision AS ENUM ('approved', 'rejected', 'on_hold');
CREATE TYPE step_result AS ENUM ('success', 'warning', 'error');
CREATE TYPE notify_status AS ENUM ('queued', 'sent', 'failed');

CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name varchar NOT NULL,
  email varchar NOT NULL UNIQUE,
  role user_role NOT NULL,
  is_active boolean NOT NULL DEFAULT true,
  password_hash varchar NOT NULL,
  must_change_password boolean NOT NULL DEFAULT false,
  temp_password_expires_at timestamptz,
  failed_login_count integer NOT NULL DEFAULT 0,
  locked_until timestamptz,
  last_login_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES users(id)
);

CREATE TABLE refresh_sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id),
  token_hash varchar(255) NOT NULL UNIQUE,
  expires_at timestamptz NOT NULL,
  is_revoked boolean NOT NULL DEFAULT false,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES users(id)
);

CREATE TABLE vendors (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name varchar(200) NOT NULL,
  tax_id varchar(30),
  is_active boolean NOT NULL DEFAULT true,
  contact_name varchar(120),
  contact_email varchar(200),
  contact_phone varchar(40),
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES users(id)
);

CREATE TABLE vendor_aliases (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  vendor_id uuid NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
  alias_norm varchar(200) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES users(id),
  CONSTRAINT uq_vendor_alias UNIQUE (vendor_id, alias_norm)
);

CREATE TABLE documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  original_filename varchar(300) NOT NULL,
  file_hash char(64) NOT NULL UNIQUE,
  storage_path varchar(500),
  parent_document_id uuid REFERENCES documents(id),
  status doc_status NOT NULL DEFAULT 'received',
  doc_type doc_type,
  raw_text text,
  extracted_json jsonb,
  email_sender varchar(200),
  email_subject varchar(500),
  email_received_at timestamptz,
  failure_message varchar(500),
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES users(id)
);

CREATE TABLE purchase_orders (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  po_number varchar(60) NOT NULL,
  version_no integer NOT NULL DEFAULT 1,
  is_current boolean NOT NULL DEFAULT true,
  vendor_id uuid REFERENCES vendors(id),
  source_document_id uuid REFERENCES documents(id),
  po_date date,
  currency char(3) NOT NULL,
  subtotal numeric(14,2),
  tax_amount numeric(14,2),
  total_amount numeric(14,2),
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES users(id),
  CONSTRAINT uq_po_version UNIQUE (po_number, version_no)
);
CREATE UNIQUE INDEX uq_current_po_number ON purchase_orders(po_number) WHERE is_current;

CREATE TABLE po_lines (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  po_id uuid NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
  item_code varchar(60),
  description varchar(300),
  uom varchar(10),
  qty numeric(14,3),
  unit_price numeric(14,4),
  line_amount numeric(14,2),
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES users(id)
);

CREATE TABLE invoices (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_document_id uuid REFERENCES documents(id),
  invoice_no varchar(60),
  version_no integer NOT NULL DEFAULT 1,
  is_current boolean NOT NULL DEFAULT true,
  vendor_id uuid REFERENCES vendors(id),
  vendor_name_raw varchar(200),
  vendor_tax_id_raw varchar(30),
  invoice_date date,
  due_date date,
  currency char(3),
  subtotal numeric(14,2),
  tax_amount numeric(14,2),
  total_amount numeric(14,2),
  po_number_raw varchar(60),
  po_id uuid REFERENCES purchase_orders(id),
  match_status match_status NOT NULL DEFAULT 'pending',
  final_status review_decision,
  final_run_id uuid,
  final_by uuid REFERENCES users(id),
  final_at timestamptz,
  owner_user_id uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES users(id),
  CONSTRAINT uq_invoice_version UNIQUE (vendor_id, invoice_no, version_no)
);
CREATE UNIQUE INDEX uq_current_vendor_invoice ON invoices(vendor_id, invoice_no)
  WHERE is_current AND vendor_id IS NOT NULL AND invoice_no IS NOT NULL;
CREATE INDEX ix_invoices_status_date ON invoices(match_status, final_status, invoice_date);
CREATE INDEX ix_invoices_due_date ON invoices(due_date);
CREATE INDEX ix_invoices_owner ON invoices(owner_user_id);
CREATE INDEX ix_invoices_po_id ON invoices(po_id);

CREATE TABLE invoice_lines (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id uuid NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  item_code varchar(60),
  description varchar(300),
  uom varchar(10),
  qty numeric(14,3),
  unit_price numeric(14,4),
  line_amount numeric(14,2),
  po_line_id uuid REFERENCES po_lines(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES users(id)
);

CREATE TABLE exchange_rates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  from_currency char(3) NOT NULL,
  to_currency char(3) NOT NULL,
  rate numeric(14,6) NOT NULL CHECK (rate > 0),
  rate_date date NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid,
  CONSTRAINT uq_exchange_rate UNIQUE(from_currency, to_currency, rate_date)
);

CREATE TABLE match_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id uuid NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  run_no integer NOT NULL,
  po_id uuid REFERENCES purchase_orders(id),
  result match_result NOT NULL,
  exchange_rate_id uuid REFERENCES exchange_rates(id),
  rules_used jsonb NOT NULL DEFAULT '{}'::jsonb,
  differences jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid,
  CONSTRAINT uq_match_run_no UNIQUE(invoice_id, run_no)
);
ALTER TABLE invoices ADD CONSTRAINT fk_invoice_final_run
  FOREIGN KEY (final_run_id) REFERENCES match_runs(id);

CREATE TABLE audit_log (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  action varchar(60) NOT NULL,
  entity_type varchar(40) NOT NULL,
  entity_id uuid,
  old_value jsonb,
  new_value jsonb,
  result step_result NOT NULL DEFAULT 'success',
  message varchar(500),
  technical_detail text,
  request_id varchar(100),
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid
);
CREATE INDEX ix_audit_entity_timeline ON audit_log(entity_type, entity_id, created_at);

CREATE TABLE notifications (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES documents(id),
  recipient_email varchar(200) NOT NULL,
  subject varchar(500),
  body text,
  status notify_status NOT NULL DEFAULT 'queued',
  attempts integer NOT NULL DEFAULT 0,
  sent_at timestamptz,
  last_error varchar(500),
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES users(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES users(id)
);
CREATE INDEX ix_documents_status ON documents(status);
CREATE INDEX ix_vendor_alias_norm ON vendor_aliases(alias_norm);
CREATE INDEX ix_refresh_user_revoked ON refresh_sessions(user_id, is_revoked);

-- Keep updated_at maintained by application/SQLAlchemy on updates.
