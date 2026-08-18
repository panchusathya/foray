/**
 * Map arbitrary source records onto the Juicebox candidate contract.
 *
 * The API does the real enrichment (titles, seniority, skills, tenure). This
 * layer only has to get the *shape* right and drop records that can't be
 * identified, so a single malformed row never fails a whole batch.
 */

const FIELD_ALIASES = {
  full_name: ['full_name', 'name', 'fullName', 'Full Name', 'candidate_name'],
  first_name: ['first_name', 'firstName', 'First Name', 'given_name'],
  last_name: ['last_name', 'lastName', 'Last Name', 'family_name', 'surname'],
  headline: ['headline', 'title', 'Headline', 'current_title', 'job_title'],
  summary: ['summary', 'bio', 'about', 'description'],
  location: ['location', 'Location', 'city_state_country', 'geo'],
  linkedin_url: ['linkedin_url', 'linkedin', 'LinkedIn', 'linkedin_profile'],
  github_url: ['github_url', 'github', 'GitHub'],
  website_url: ['website_url', 'website', 'blog', 'personal_site'],
  current_company: ['current_company', 'company', 'Company', 'employer'],
};

const listSeparators = /[;,|]/;

function pick(record, key) {
  for (const alias of FIELD_ALIASES[key] ?? [key]) {
    const value = record[alias];
    if (value !== undefined && value !== null && String(value).trim() !== '') {
      return String(value).trim();
    }
  }
  return '';
}

function toList(value) {
  if (Array.isArray(value)) return value.map((v) => String(v).trim()).filter(Boolean);
  if (typeof value === 'string') {
    return value.split(listSeparators).map((v) => v.trim()).filter(Boolean);
  }
  return [];
}

function collectEmails(record) {
  const raw = record.emails ?? record.email ?? record.Email ?? record.work_email ?? [];
  return toList(raw)
    .map((value) => value.toLowerCase())
    .filter((value) => /^[^@\s]+@[^@\s.]+\.[^@\s]+$/.test(value));
}

function normalizeExperience(entry) {
  if (!entry || typeof entry !== 'object') return null;
  const company = String(entry.company ?? entry.organization ?? entry.employer ?? '').trim();
  const title = String(entry.title ?? entry.role ?? entry.position ?? '').trim();
  if (!company && !title) return null;
  return {
    company,
    company_domain: String(entry.company_domain ?? entry.domain ?? '').trim().toLowerCase(),
    company_industry: String(entry.company_industry ?? entry.industry ?? '').trim(),
    company_size: String(entry.company_size ?? entry.headcount ?? '').trim(),
    company_stage: String(entry.company_stage ?? entry.stage ?? '').trim(),
    title,
    start_date: String(entry.start_date ?? entry.starts_at ?? entry.from ?? '').trim(),
    end_date: String(entry.end_date ?? entry.ends_at ?? entry.to ?? '').trim(),
    is_current: Boolean(entry.is_current ?? entry.current ?? false),
    location: String(entry.location ?? '').trim(),
    description: String(entry.description ?? entry.summary ?? '').trim(),
  };
}

function normalizeEducation(entry) {
  if (!entry || typeof entry !== 'object') return null;
  const school = String(entry.school ?? entry.institution ?? entry.university ?? '').trim();
  if (!school) return null;
  const year = (value) => {
    const parsed = Number.parseInt(String(value ?? '').slice(0, 4), 10);
    return Number.isFinite(parsed) ? parsed : null;
  };
  return {
    school,
    degree: String(entry.degree ?? '').trim(),
    field_of_study: String(entry.field_of_study ?? entry.field ?? entry.major ?? '').trim(),
    start_year: year(entry.start_year ?? entry.starts_at),
    end_year: year(entry.end_year ?? entry.ends_at ?? entry.graduation_year),
  };
}

/**
 * @returns {object|null} a Juicebox candidate payload, or null if unusable.
 */
export function toCandidate(record, { source = 'ingest', sourceIdField = 'id' } = {}) {
  if (!record || typeof record !== 'object') return null;

  let fullName = pick(record, 'full_name');
  const first = pick(record, 'first_name');
  const last = pick(record, 'last_name');
  if (!fullName) fullName = [first, last].filter(Boolean).join(' ').trim();
  if (!fullName) return null;

  const emails = collectEmails(record);
  const linkedin = pick(record, 'linkedin_url');
  const sourceId = String(
    record[sourceIdField] ?? record.source_id ?? linkedin ?? emails[0] ?? fullName.toLowerCase(),
  ).trim();

  const experiences = toArray(record.experiences ?? record.positions ?? record.jobs)
    .map(normalizeExperience)
    .filter(Boolean);

  // Flat CSV rows only carry the current role — synthesise one so tenure and
  // company signals still work downstream.
  const flatCompany = pick(record, 'current_company');
  const flatTitle = pick(record, 'headline');
  if (experiences.length === 0 && (flatCompany || flatTitle)) {
    experiences.push({
      company: flatCompany,
      company_domain: String(record.company_domain ?? '').trim().toLowerCase(),
      company_industry: String(record.industry ?? '').trim(),
      company_size: String(record.company_size ?? '').trim(),
      company_stage: String(record.company_stage ?? '').trim(),
      title: flatTitle,
      start_date: String(record.started_at ?? record.start_date ?? '').trim(),
      end_date: '',
      is_current: true,
      location: pick(record, 'location'),
      description: '',
    });
  }

  return {
    source,
    source_id: sourceId,
    full_name: fullName,
    first_name: first,
    last_name: last,
    headline: pick(record, 'headline'),
    summary: pick(record, 'summary'),
    location: pick(record, 'location'),
    location_city: String(record.location_city ?? record.city ?? '').trim(),
    location_region: String(record.location_region ?? record.state ?? record.region ?? '').trim(),
    location_country: String(record.location_country ?? record.country ?? '').trim(),
    open_to_remote: Boolean(record.open_to_remote ?? record.remote ?? false),
    linkedin_url: linkedin,
    github_url: pick(record, 'github_url'),
    website_url: pick(record, 'website_url'),
    emails,
    phones: toList(record.phones ?? record.phone ?? []),
    skills: toList(record.skills ?? record.Skills ?? []),
    industries: toList(record.industries ?? record.industry ?? []),
    experiences,
    educations: toArray(record.educations ?? record.education)
      .map(normalizeEducation)
      .filter(Boolean),
    raw: { ingested_at: new Date().toISOString(), source },
  };
}

function toArray(value) {
  if (Array.isArray(value)) return value;
  if (value && typeof value === 'object') return [value];
  return [];
}

/** De-duplicate a batch on (source, source_id) — sources repeat rows constantly. */
export function dedupe(candidates) {
  const seen = new Map();
  for (const candidate of candidates) {
    if (!candidate) continue;
    seen.set(`${candidate.source}:${candidate.source_id}`, candidate);
  }
  return [...seen.values()];
}
