#!/usr/bin/env node
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'

const stage = resolve(process.argv[2] ?? 'dist/packages')
const output = resolve(process.argv[3] ?? 'dist/release/dsh-cloud.spdx.json')
const manifest = JSON.parse(await readFile(resolve(stage, 'npm/release-manifest.json'), 'utf8'))
const npmPackage = JSON.parse(await readFile(resolve(stage, 'npm/package.json'), 'utf8'))
const created = new Date(Number.parseInt(process.env.SOURCE_DATE_EPOCH ?? '0', 10) * 1000).toISOString()
const namespace = `https://github.com/AgentsDanceAI/deepseek-harness-cloud/sbom/${manifest.version}`

const packageRecord = (name, spdxId, purl) => ({
  SPDXID: spdxId,
  name,
  versionInfo: manifest.version,
  downloadLocation: 'NOASSERTION',
  filesAnalyzed: false,
  licenseConcluded: manifest.license,
  licenseDeclared: manifest.license,
  copyrightText: 'Copyright AgentsDance AI and contributors',
  externalRefs: [{
    referenceCategory: 'PACKAGE-MANAGER',
    referenceType: 'purl',
    referenceLocator: purl,
  }],
})

const document = {
  spdxVersion: 'SPDX-2.3',
  dataLicense: 'CC0-1.0',
  SPDXID: 'SPDXRef-DOCUMENT',
  name: `DSH Cloud CLI ${manifest.version}`,
  documentNamespace: namespace,
  creationInfo: {
    created,
    creators: ['Organization: AgentsDance AI', 'Tool: dsh-cloud-generate-sbom'],
  },
  packages: [
    packageRecord(
      npmPackage.name,
      'SPDXRef-Package-npm',
      `pkg:npm/%40agentsdanceai/dsh-cloud@${manifest.version}`,
    ),
    packageRecord(
      'dsh-cloud',
      'SPDXRef-Package-python',
      `pkg:pypi/dsh-cloud@${manifest.version}`,
    ),
  ],
  relationships: [
    { spdxElementId: 'SPDXRef-DOCUMENT', relationshipType: 'DESCRIBES', relatedSpdxElement: 'SPDXRef-Package-npm' },
    { spdxElementId: 'SPDXRef-DOCUMENT', relationshipType: 'DESCRIBES', relatedSpdxElement: 'SPDXRef-Package-python' },
  ],
}

await mkdir(dirname(output), { recursive: true })
await writeFile(output, `${JSON.stringify(document, null, 2)}\n`)
process.stdout.write(`${output}\n`)
