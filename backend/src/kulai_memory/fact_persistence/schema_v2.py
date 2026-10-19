"""Durable generations and transactional source invalidation; owned-only DDL."""

from .schema_v1 import TABLES as V1_TABLES

FACT_HEAD = "kulai_facts_0002"
TABLES = (*V1_TABLES, "kulai_fact_memory_epochs")

DDL = (
    """DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM public.memories m JOIN public.memory_ingestion_tombstones t ON t.memory_id=m.id) THEN
            RAISE EXCEPTION 'Canonical retired identity overlap requires review';
        END IF;
        END $$""",
    # Retirements deliberately survive Memory DELETE. No text, hashes or quotes.
    """CREATE TABLE public.kulai_fact_memory_epochs (
        memory_id uuid PRIMARY KEY, incarnation uuid NOT NULL DEFAULT gen_random_uuid(),
        generation bigint NOT NULL DEFAULT 1 CHECK(generation>=1), deleted_at timestamptz
    )""",
    """INSERT INTO public.kulai_fact_memory_epochs(memory_id) SELECT id FROM public.memories""",
    """INSERT INTO public.kulai_fact_memory_epochs(memory_id,deleted_at)
        SELECT memory_id,deleted_at FROM public.memory_ingestion_tombstones""",
    """CREATE INDEX ix_fact_versions_live_source ON public.kulai_fact_source_versions(source_id,id)
        WHERE invalidated_at IS NULL""",
    """CREATE FUNCTION public.kulai_fact_epoch_guard() RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog,public AS $$ BEGIN
        IF TG_OP IN ('DELETE','TRUNCATE') THEN
            RAISE EXCEPTION 'Fact generation retirements cannot be removed';
        END IF;
        IF NEW.memory_id<>OLD.memory_id OR NEW.incarnation<>OLD.incarnation
           OR NEW.generation<>OLD.generation+1 OR OLD.deleted_at IS NOT NULL THEN
            RAISE EXCEPTION 'Invalid durable source generation transition';
        END IF;
        RETURN NEW; END $$""",
    """CREATE TRIGGER fact_epoch_guard BEFORE UPDATE OR DELETE ON public.kulai_fact_memory_epochs
        FOR EACH ROW EXECUTE FUNCTION public.kulai_fact_epoch_guard()""",
    """CREATE TRIGGER fact_epoch_no_truncate BEFORE TRUNCATE ON public.kulai_fact_memory_epochs
        FOR EACH STATEMENT EXECUTE FUNCTION public.kulai_fact_epoch_guard()""",
    """CREATE FUNCTION public.kulai_fact_invalidate_memory(
        changed_memory uuid, keep_token text, reason text) RETURNS void LANGUAGE plpgsql
        SET search_path=pg_catalog,public AS $$
        DECLARE ver record; cand record; rel record; affected uuid[] := ARRAY[]::uuid[];
        BEGIN
        FOR ver IN SELECT v.id FROM public.kulai_fact_source_versions v
            JOIN public.kulai_fact_sources s ON s.id=v.source_id
            WHERE s.memory_id=changed_memory AND v.invalidated_at IS NULL
              AND (keep_token IS NULL OR v.source_token<>keep_token)
            ORDER BY v.id FOR UPDATE OF v
        LOOP
            UPDATE public.kulai_fact_source_versions SET invalidated_at=clock_timestamp(),
                invalidation_reason=reason WHERE id=ver.id;
            INSERT INTO public.kulai_fact_decisions
                (request_id,target_id,target_kind,action,reviewer_id,expected_version,resulting_version)
                VALUES(gen_random_uuid(),ver.id,'source_version','invalidate','system:source-invalidated',0,0);
            FOR cand IN SELECT id,status,lock_version FROM public.kulai_fact_candidates
                WHERE source_version_id=ver.id ORDER BY id FOR UPDATE
            LOOP
                affected := array_append(affected,cand.id);
                IF cand.status='APPROVED' THEN
                    UPDATE public.kulai_fact_candidates SET status='REVOKED',lock_version=lock_version+1 WHERE id=cand.id;
                    UPDATE public.kulai_fact_approvals SET revoked_at=clock_timestamp(),revocation_reason=reason
                        WHERE candidate_id=cand.id AND revoked_at IS NULL;
                    INSERT INTO public.kulai_fact_decisions
                        (request_id,target_id,target_kind,action,reviewer_id,expected_version,resulting_version)
                        VALUES(gen_random_uuid(),cand.id,'candidate','invalidate','system:source-invalidated',cand.lock_version,cand.lock_version+1);
                END IF;
            END LOOP;
        END LOOP;
        FOR rel IN SELECT id,lock_version FROM public.kulai_fact_relations
            WHERE status='APPROVED' AND (left_id=ANY(affected) OR right_id=ANY(affected))
            ORDER BY id FOR UPDATE
        LOOP
            UPDATE public.kulai_fact_relations SET status='REVOKED',lock_version=lock_version+1 WHERE id=rel.id;
            INSERT INTO public.kulai_fact_decisions
                (request_id,target_id,target_kind,action,reviewer_id,expected_version,resulting_version)
                VALUES(gen_random_uuid(),rel.id,'relation','invalidate','system:source-invalidated',rel.lock_version,rel.lock_version+1);
        END LOOP;
        END $$""",
    """CREATE FUNCTION public.kulai_fact_memory_lifecycle() RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog,public AS $$
        DECLARE next_token text;
        BEGIN
        IF TG_OP='TRUNCATE' THEN
            RAISE EXCEPTION 'Use transactional Memory DELETE, not TRUNCATE';
        ELSIF TG_OP='INSERT' THEN
            IF EXISTS(SELECT 1 FROM public.kulai_fact_memory_epochs WHERE memory_id=NEW.id) THEN
                RAISE EXCEPTION 'Retired Memory identity cannot be reused';
            END IF;
            INSERT INTO public.kulai_fact_memory_epochs(memory_id) VALUES(NEW.id);
            RETURN NEW;
        ELSIF TG_OP='DELETE' THEN
            UPDATE public.kulai_fact_memory_epochs SET generation=generation+1,deleted_at=clock_timestamp()
                WHERE memory_id=OLD.id AND deleted_at IS NULL;
            IF NOT FOUND THEN RAISE EXCEPTION 'Missing durable source generation'; END IF;
            PERFORM public.kulai_fact_invalidate_memory(OLD.id,NULL,'source_deleted');
            RETURN OLD;
        END IF;
        IF NEW.id<>OLD.id THEN RAISE EXCEPTION 'Memory identity is immutable'; END IF;
        IF NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
        UPDATE public.kulai_fact_memory_epochs SET generation=generation+1
            WHERE memory_id=OLD.id AND deleted_at IS NULL
            RETURNING 'epoch:'||incarnation::text||':'||generation::text INTO next_token;
        IF NOT FOUND THEN RAISE EXCEPTION 'Missing durable source generation'; END IF;
        PERFORM public.kulai_fact_invalidate_memory(OLD.id,next_token,
            CASE WHEN NEW.archived_at IS NOT NULL THEN 'source_archived'
                 WHEN OLD.archived_at IS NOT NULL THEN 'source_restored' ELSE 'source_changed' END);
        RETURN NEW;
        END $$""",
    """CREATE TRIGGER fact_memory_insert AFTER INSERT ON public.memories
        FOR EACH ROW EXECUTE FUNCTION public.kulai_fact_memory_lifecycle()""",
    """CREATE TRIGGER fact_memory_update AFTER UPDATE ON public.memories
        FOR EACH ROW EXECUTE FUNCTION public.kulai_fact_memory_lifecycle()""",
    """CREATE TRIGGER fact_memory_delete BEFORE DELETE ON public.memories
        FOR EACH ROW EXECUTE FUNCTION public.kulai_fact_memory_lifecycle()""",
    """CREATE TRIGGER fact_memory_no_truncate BEFORE TRUNCATE ON public.memories
        FOR EACH STATEMENT EXECUTE FUNCTION public.kulai_fact_memory_lifecycle()""",
)

# The ONLY use of transaction identity in v2 is this one-time legacy conversion.
# Keep supported v1 approvals; conservatively invalidate unverifiable old data.
CONVERT_LEGACY = """UPDATE public.kulai_fact_source_versions v
    SET source_token='epoch:'||e.incarnation::text||':'||e.generation::text
    FROM public.kulai_fact_sources s JOIN public.memories m ON m.id=s.memory_id
    JOIN public.kulai_fact_memory_epochs e ON e.memory_id=m.id
    WHERE v.source_id=s.id AND v.invalidated_at IS NULL AND m.archived_at IS NULL
      AND m.revision=v.revision AND m.xmin::text=v.source_token
      AND encode(sha256(convert_to(m.content,'UTF8')),'hex')=v.sha256
      AND NOT EXISTS(SELECT 1 FROM public.memory_ingestion_tombstones t WHERE t.memory_id=m.id)"""

INVALIDATE_LEGACY = """SELECT public.kulai_fact_invalidate_memory(memory_id,
    'epoch:'||incarnation::text||':'||generation::text,'epoch_migration')
    FROM public.kulai_fact_memory_epochs ORDER BY memory_id"""
