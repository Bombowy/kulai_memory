"""Immutable, separately migrated fact storage contract. Never install at startup."""

FACT_HEAD = "kulai_facts_0001"
VERSION_TABLE = "kulai_facts_version"
TABLES = (
    "kulai_fact_sources", "kulai_fact_source_versions", "kulai_fact_identities",
    "kulai_fact_candidates", "kulai_fact_approvals", "kulai_fact_relations", "kulai_fact_decisions",
)

DDL = (
    """CREATE TABLE public.kulai_fact_sources (
        id uuid PRIMARY KEY,
        memory_id uuid UNIQUE REFERENCES public.memories(id) ON DELETE CASCADE,
        local_key uuid UNIQUE, local_text text, local_revision integer NOT NULL DEFAULT 1,
        local_token uuid NOT NULL DEFAULT gen_random_uuid(),
        CONSTRAINT ck_fact_source_kind CHECK (
            (memory_id IS NOT NULL AND local_key IS NULL AND local_text IS NULL) OR
            (memory_id IS NULL AND local_key=id AND local_text IS NOT NULL)),
        CONSTRAINT ck_fact_local_revision CHECK(local_revision>=1),
        CONSTRAINT ck_fact_local_size CHECK(local_text IS NULL OR char_length(local_text)<=12000)
    )""",
    """CREATE TABLE public.kulai_fact_source_versions (
        id uuid PRIMARY KEY,
        source_id uuid NOT NULL REFERENCES public.kulai_fact_sources(id) ON DELETE CASCADE,
        number integer NOT NULL CHECK(number>=1), revision integer NOT NULL CHECK(revision>=1),
        sha256 text NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'), source_token text NOT NULL,
        document_meta jsonb NOT NULL CHECK(jsonb_typeof(document_meta)='object' AND NOT (document_meta ? 'text')),
        pipeline text NOT NULL, staged_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        invalidated_at timestamptz, invalidation_reason text,
        UNIQUE(source_id,number), UNIQUE(source_id,revision,source_token)
    )""",
    """CREATE TABLE public.kulai_fact_identities (
        id uuid PRIMARY KEY,
        source_id uuid NOT NULL REFERENCES public.kulai_fact_sources(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE public.kulai_fact_candidates (
        id uuid PRIMARY KEY,
        fact_id uuid NOT NULL REFERENCES public.kulai_fact_identities(id) ON DELETE CASCADE,
        source_version_id uuid NOT NULL REFERENCES public.kulai_fact_source_versions(id) ON DELETE CASCADE,
        version integer NOT NULL CHECK(version>=1), ordinal integer NOT NULL CHECK(ordinal BETWEEN 1 AND 20),
        information_type text NOT NULL CHECK(information_type IN ('fact','preference','plan','hypothesis')),
        payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND pg_column_size(payload)<=262144),
        status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPROVED','REJECTED','REVOKED')),
        lock_version integer NOT NULL DEFAULT 1 CHECK(lock_version>=1),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(fact_id,version), UNIQUE(source_version_id,ordinal)
    )""",
    """CREATE INDEX ix_fact_candidates_status_id ON public.kulai_fact_candidates(status,id)""",
    """CREATE INDEX ix_fact_candidates_source_version ON public.kulai_fact_candidates(source_version_id,id)""",
    """CREATE INDEX ix_fact_identities_source ON public.kulai_fact_identities(source_id,id)""",
    """CREATE TABLE public.kulai_fact_approvals (
        candidate_id uuid PRIMARY KEY REFERENCES public.kulai_fact_candidates(id) ON DELETE CASCADE,
        approved_at timestamptz NOT NULL, reviewer_id text NOT NULL,
        revoked_at timestamptz, revocation_reason text
    )""",
    """CREATE TABLE public.kulai_fact_relations (
        id uuid PRIMARY KEY,
        left_id uuid NOT NULL REFERENCES public.kulai_fact_candidates(id) ON DELETE CASCADE,
        right_id uuid NOT NULL REFERENCES public.kulai_fact_candidates(id) ON DELETE CASCADE,
        relation text NOT NULL CHECK(relation IN ('exact_duplicate','semantic_duplicate','related','possible_conflict','possible_supersession')),
        payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
        status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPROVED','REJECTED','REVOKED')),
        lock_version integer NOT NULL DEFAULT 1 CHECK(lock_version>=1),
        CHECK(left_id<>right_id), UNIQUE(left_id,right_id,relation)
    )""",
    """CREATE INDEX ix_fact_relations_status_id ON public.kulai_fact_relations(status,id)""",
    """CREATE INDEX ix_fact_relations_right ON public.kulai_fact_relations(right_id,id)""",
    """CREATE TABLE public.kulai_fact_decisions (
        request_id uuid PRIMARY KEY,
        target_id uuid NOT NULL, target_kind text NOT NULL CHECK(target_kind IN ('candidate','relation','source','source_version')),
        action text NOT NULL CHECK(action IN ('approve','reject','revoke','invalidate','source_deleted')),
        reviewer_id text NOT NULL CHECK(char_length(reviewer_id) BETWEEN 1 AND 200),
        decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        expected_version integer NOT NULL, resulting_version integer NOT NULL
    )""",
    """CREATE INDEX ix_fact_decisions_target ON public.kulai_fact_decisions(target_id,decided_at,request_id)""",
    """CREATE FUNCTION public.kulai_fact_immutable_decision() RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog,public AS $$ BEGIN
            RAISE EXCEPTION 'Fact decisions are append-only';
        END $$""",
    """CREATE TRIGGER immutable_fact_decisions BEFORE UPDATE OR DELETE ON public.kulai_fact_decisions
        FOR EACH ROW EXECUTE FUNCTION public.kulai_fact_immutable_decision()""",
    """CREATE FUNCTION public.kulai_fact_candidate_guard() RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog,public AS $$ BEGIN
        IF (NEW.payload,NEW.fact_id,NEW.source_version_id,NEW.version,NEW.ordinal,NEW.information_type,NEW.created_at)
            IS DISTINCT FROM
           (OLD.payload,OLD.fact_id,OLD.source_version_id,OLD.version,OLD.ordinal,OLD.information_type,OLD.created_at)
        THEN RAISE EXCEPTION 'Fact candidate content is immutable'; END IF;
        IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
           (OLD.status='PENDING' AND NEW.status IN ('APPROVED','REJECTED')) OR
           (OLD.status='APPROVED' AND NEW.status='REVOKED'))
        THEN RAISE EXCEPTION 'Invalid fact transition'; END IF;
        IF NEW.lock_version <> OLD.lock_version + 1 THEN
            RAISE EXCEPTION 'Invalid fact lock version'; END IF;
        RETURN NEW; END $$""",
    """CREATE TRIGGER immutable_fact_candidate BEFORE UPDATE ON public.kulai_fact_candidates
        FOR EACH ROW EXECUTE FUNCTION public.kulai_fact_candidate_guard()""",
    """CREATE FUNCTION public.kulai_fact_version_guard() RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog,public AS $$ BEGIN
        IF (NEW.id,NEW.source_id,NEW.number,NEW.revision,NEW.sha256,NEW.source_token,NEW.document_meta,NEW.pipeline,NEW.staged_at)
            IS DISTINCT FROM
           (OLD.id,OLD.source_id,OLD.number,OLD.revision,OLD.sha256,OLD.source_token,OLD.document_meta,OLD.pipeline,OLD.staged_at)
           OR (OLD.invalidated_at IS NOT NULL AND (NEW.invalidated_at,NEW.invalidation_reason)
               IS DISTINCT FROM (OLD.invalidated_at,OLD.invalidation_reason))
        THEN RAISE EXCEPTION 'Fact source versions are immutable'; END IF;
        RETURN NEW; END $$""",
    """CREATE TRIGGER immutable_fact_source_version BEFORE UPDATE ON public.kulai_fact_source_versions
        FOR EACH ROW EXECUTE FUNCTION public.kulai_fact_version_guard()""",
    """CREATE FUNCTION public.kulai_fact_approval_guard() RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog,public AS $$ BEGIN
        IF (NEW.candidate_id,NEW.approved_at,NEW.reviewer_id) IS DISTINCT FROM (OLD.candidate_id,OLD.approved_at,OLD.reviewer_id)
           OR OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL
        THEN RAISE EXCEPTION 'Fact approval versions are immutable'; END IF;
        RETURN NEW; END $$""",
    """CREATE TRIGGER immutable_fact_approval BEFORE UPDATE ON public.kulai_fact_approvals
        FOR EACH ROW EXECUTE FUNCTION public.kulai_fact_approval_guard()""",
    """CREATE FUNCTION public.kulai_fact_relation_guard() RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog,public AS $$ BEGIN
        IF (NEW.id,NEW.left_id,NEW.right_id,NEW.relation,NEW.payload) IS DISTINCT FROM
           (OLD.id,OLD.left_id,OLD.right_id,OLD.relation,OLD.payload)
           OR NEW.lock_version<>OLD.lock_version+1
           OR NOT ((OLD.status='PENDING' AND NEW.status IN ('APPROVED','REJECTED')) OR
                   (OLD.status='APPROVED' AND NEW.status='REVOKED'))
        THEN RAISE EXCEPTION 'Invalid fact relation transition'; END IF;
        RETURN NEW; END $$""",
    """CREATE TRIGGER immutable_fact_relation BEFORE UPDATE ON public.kulai_fact_relations
        FOR EACH ROW EXECUTE FUNCTION public.kulai_fact_relation_guard()""",
    """CREATE FUNCTION public.kulai_fact_purge_audit() RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog,public AS $$ BEGIN
        INSERT INTO public.kulai_fact_decisions(request_id,target_id,target_kind,action,reviewer_id,expected_version,resulting_version)
          VALUES(gen_random_uuid(),OLD.id,TG_ARGV[0],'source_deleted','system:source-delete',0,0);
        RETURN OLD; END $$""",
    """CREATE TRIGGER fact_source_purge BEFORE DELETE ON public.kulai_fact_sources
        FOR EACH ROW EXECUTE FUNCTION public.kulai_fact_purge_audit('source')""",
    """CREATE TRIGGER fact_candidate_purge BEFORE DELETE ON public.kulai_fact_candidates
        FOR EACH ROW EXECUTE FUNCTION public.kulai_fact_purge_audit('candidate')""",
    """CREATE TRIGGER fact_relation_purge BEFORE DELETE ON public.kulai_fact_relations
        FOR EACH ROW EXECUTE FUNCTION public.kulai_fact_purge_audit('relation')""",
)
