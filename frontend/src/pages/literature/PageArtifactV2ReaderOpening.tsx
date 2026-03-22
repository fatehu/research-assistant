import { Collapse, Typography } from 'antd'
import { ProCard } from '@ant-design/pro-components'

const { Paragraph, Title } = Typography

type PageArtifactV2ReaderOpeningProps = {
  title: string
  summary?: string
  points?: string[]
  previousBridgeLabel?: string
  previousBridgeSummary?: string
  previousBridgePoints?: string[]
  quote?: string
  pageNumber?: number
}

export default function PageArtifactV2ReaderOpening(props: PageArtifactV2ReaderOpeningProps) {
  const points = Array.isArray(props.points) ? props.points.filter(Boolean) : []
  const previousBridgePoints = Array.isArray(props.previousBridgePoints)
    ? props.previousBridgePoints.filter(Boolean)
    : []
  const quote = String(props.quote || '').trim()

  return (
    <ProCard ghost className="page-artifact-v2__reader-opening" bodyStyle={{ padding: 0 }}>
      <div className="page-artifact-v2__reader-opening-copy">
        <header id="reader-opening" className="page-artifact-v2__reader-opening-meta">
          {props.pageNumber ? (
            <span className="page-artifact-v2__page-badge">PAGE {props.pageNumber}</span>
          ) : null}
          <div className="page-artifact-v2__opening-accent-line" />
        </header>

        <Title className="page-artifact-v2__opening-title">{props.title}</Title>

        <div className="page-artifact-v2__reader-opening-body">
          {props.summary ? (
            <Paragraph className="page-artifact-v2__opening-summary">{props.summary}</Paragraph>
          ) : null}

          {points.length ? (
            <div className="page-artifact-v2__hero-points">
              {points.map((item, idx) => (
                <div key={`${item}-${idx}`} className="page-artifact-v2__hero-point">
                  <span className="page-artifact-v2__hero-point-dot" />
                  <span>{item}</span>
                </div>
              ))}
            </div>
          ) : null}

          {props.previousBridgeSummary || previousBridgePoints.length ? (
            <div className="page-artifact-v2__reader-opening-bridge">
              <span className="page-artifact-v2__hero-bridge-kicker">
                {props.previousBridgeLabel || '承接上一页'}
              </span>
              {props.previousBridgeSummary ? (
                <Paragraph className="page-artifact-v2__hero-context">
                  {props.previousBridgeSummary}
                </Paragraph>
              ) : null}
              {previousBridgePoints.length ? (
                <div className="page-artifact-v2__outro-points">
                  {previousBridgePoints.map((item, idx) => (
                    <div key={`${item}-${idx}`} className="page-artifact-v2__hero-note">
                      <span className="page-artifact-v2__hero-note-dot" />
                      <span>{item}</span>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}

          {quote ? (
            <Collapse
              ghost
              size="small"
              className="page-artifact-v2__reader-opening-quote"
              items={[
                {
                  key: 'opening-quote',
                  label: '关键原文锚点',
                  children: (
                    <Paragraph className="page-artifact-v2__hero-quote-text">
                      {quote}
                    </Paragraph>
                  ),
                },
              ]}
            />
          ) : null}
        </div>
      </div>
    </ProCard>
  )
}
